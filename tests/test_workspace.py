from __future__ import annotations

import pytest

from flaggems_rsi.workspace import WorkspaceError, WorkspaceManager, validate_patch


def test_untracked_file_capture_and_replay(config):
    run = config.repository.parent / "run"
    run.mkdir()
    config.allowed_files.append("new_kernel.py")
    manager = WorkspaceManager(config, run)
    source = run / "source"
    workspace = run / "candidate"
    manager.create(type("Node", (), {
        "base_revision": "HEAD", "parent_ids": [], "patch_path": None, "parent_patch_paths": [],
    })(), source)
    (source / "new_kernel.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = manager.capture(source)
    assert b"new file mode" in patch
    validate_patch(patch, config.allowed_files)
    manager.create(type("Node", (), {
        "base_revision": "HEAD", "parent_ids": [], "patch_path": None, "parent_patch_paths": [],
    })(), workspace)
    manager.apply(workspace, patch)
    assert (workspace / "new_kernel.py").exists()


def test_forbidden_patch_is_rejected():
    patch = b"diff --git a/harness.py b/harness.py\n--- a/harness.py\n+++ b/harness.py\n"
    with pytest.raises(WorkspaceError, match="outside"):
        validate_patch(patch, ["kernel.py"])
    traversal = b"diff --git a/../secret b/../secret\n"
    with pytest.raises(ValueError, match="safe relative"):
        validate_patch(traversal, ["kernel.py"])


def test_source_repository_is_never_a_run_child(config):
    with pytest.raises(WorkspaceError, match="must not be inside"):
        WorkspaceManager(config, config.repository / "run")
