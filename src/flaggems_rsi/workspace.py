"""Git patch capture and isolated, non-committing candidate workspaces."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .schemas import IterationNode, RunConfig
from .storage import ensure_relative


class WorkspaceError(RuntimeError):
    pass


def git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, check=False,
    )
    if check and result.returncode:
        raise WorkspaceError(result.stderr.decode(errors="replace") or "git command failed")
    return result


def resolve_revision(repository: Path, revision: str) -> str:
    return git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.decode().strip()


def _patch_paths(patch: bytes) -> set[str]:
    paths: set[str] = set()
    lines = patch.decode("utf-8", errors="surrogateescape").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^diff --git (?:\"a/(.*)\"|a/(\S+)) (?:\"b/(.*)\"|b/(\S+))$", line)
        if match:
            candidates = (match.group(1) or match.group(2), match.group(3) or match.group(4))
        else:
            # New-file --no-index patches have absolute destination paths. The
            # immediately following +++ header identifies the relative path.
            no_index = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
            if not no_index:
                continue
            following = lines[index + 1:index + 4]
            destination = next((item[4:].strip() for item in following if item.startswith("+++ ")), "")
            if destination.startswith("b/"):
                candidates = (destination[2:],)
            else:
                candidates = (no_index.group(1),)
        for value in candidates:
            if value.startswith("\\") or "\\" in value:
                value = bytes(value, "utf-8").decode("unicode_escape")
            path = ensure_relative(value)
            if str(path) != ".":
                paths.add(str(path))
    if not patch:
        return set()
    if not paths:
        raise WorkspaceError("No safe file paths found in patch")
    return paths


def validate_patch(patch: bytes, allowed_files: list[str]) -> None:
    allowed = set(allowed_files)
    if forbidden := _patch_paths(patch) - allowed:
        raise WorkspaceError(f"Patch modifies files outside the allowed set: {sorted(forbidden)}")


def _clean_patch(path: Path) -> bytes:
    result = path.read_bytes()
    if path.suffix == ".patch":
        return result
    # A sidecar is not an implicit executable operation; only an explicitly
    # configured, validated patch artifact is accepted.
    raise WorkspaceError(f"Unsupported patch artifact: {path}")


class WorkspaceManager:
    def __init__(self, config: RunConfig, work_root: Path):
        self.config = config
        self.repository = config.repository.resolve()
        self.work_root = work_root.resolve()
        if not (self.repository / ".git").exists():
            raise WorkspaceError(f"Not a Git repository: {self.repository}")
        if self._is_within(self.repository, self.work_root):
            raise WorkspaceError("Run workspaces must not be inside the source repository")

    @staticmethod
    def _is_within(parent: Path, child: Path) -> bool:
        return child == parent or parent in child.parents

    def _check_destination(self, destination: Path) -> None:
        destination = destination.resolve()
        if not self._is_within(self.work_root, destination):
            raise WorkspaceError("Candidate workspace escapes the run directory")
        if destination.exists():
            raise WorkspaceError(f"Refusing to overwrite existing workspace: {destination}")

    def create(self, node: IterationNode, destination: Path) -> Path:
        self._check_destination(destination)
        base = resolve_revision(self.repository, node.base_revision)
        git(self.repository, "worktree", "add", "--detach", str(destination), base)
        try:
            patch_paths = list(node.parent_patch_paths)
            if node.patch_path and node.patch_path not in patch_paths:
                patch_paths.append(node.patch_path)
            for parent_patch in patch_paths:
                patch = (self.work_root / "artifacts" / parent_patch).resolve()
                if not self._is_within(self.work_root / "artifacts" / "patches", patch):
                    raise WorkspaceError("Patch path escapes artifact directory")
                data = _clean_patch(patch)
                validate_patch(data, self.config.allowed_files)
                self.apply(destination, data)
        except Exception:
            git(self.repository, "worktree", "remove", "--force", str(destination), check=False)
            raise
        return destination

    def apply(self, workspace: Path, patch: bytes) -> None:
        validate_patch(patch, self.config.allowed_files)
        check = subprocess.run(
            ["git", "-C", str(workspace), "apply", "--check", "--whitespace=nowarn", "-"],
            input=patch, capture_output=True, check=False,
        )
        if check.returncode:
            raise WorkspaceError(check.stderr.decode(errors="replace"))
        result = subprocess.run(
            ["git", "-C", str(workspace), "apply", "--whitespace=nowarn", "-"],
            input=patch, capture_output=True, check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.decode(errors="replace"))

    def reverse(self, workspace: Path, patch: bytes) -> None:
        validate_patch(patch, self.config.allowed_files)
        result = subprocess.run(
            ["git", "-C", str(workspace), "apply", "--reverse", "--whitespace=nowarn", "-"],
            input=patch, capture_output=True, check=False,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.decode(errors="replace"))

    def capture(self, workspace: Path) -> bytes:
        if not self._is_within(self.work_root, workspace.resolve()):
            raise WorkspaceError("Cannot capture outside the run workspace")
        for filename in self.config.allowed_files:
            path = ensure_relative(filename)
            if (workspace / ".git").resolve() in (workspace / path).resolve().parents:
                raise WorkspaceError("Cannot modify Git metadata")
        # --no-index output for untracked files is converted into a regular patch
        # against /dev/null; paths stay within allowed source files.
        patch = bytearray(git(workspace, "diff", "--binary", "HEAD", "--", *self.config.allowed_files).stdout)
        untracked = git(workspace, "ls-files", "--others", "--exclude-standard", "--",
                        *self.config.allowed_files).stdout.decode().splitlines()
        for filename in untracked:
            ensure_relative(filename)
            result = subprocess.run(
                ["git", "-C", str(workspace), "diff", "--binary", "--no-index", "--", "/dev/null", filename],
                capture_output=True, check=False,
            )
            if result.returncode != 1:
                raise WorkspaceError(result.stderr.decode(errors="replace") or "Failed to capture new file")
            patch_text = result.stdout.decode("utf-8", errors="surrogateescape")
            absolute = str((workspace / filename).resolve())
            patch_text = patch_text.replace(absolute, f"b/{filename}")
            patch.extend(patch_text.encode("utf-8", errors="surrogateescape"))
        validate_patch(bytes(patch), self.config.allowed_files)
        return bytes(patch)

    def candidate_revision(self, workspace: Path, patch_id: str) -> str:
        revision = resolve_revision(workspace, "HEAD")
        return f"{revision}+patch:{patch_id}"

    def check_scope(self, workspace: Path) -> None:
        self.capture(workspace)

    def dispose(self, workspace: Path) -> None:
        if self._is_within(self.work_root, workspace.resolve()) and workspace.exists():
            git(self.repository, "worktree", "remove", "--force", str(workspace))
