"""Immutable experiment graph, Champion pointers, and lineage queries."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .schemas import (
    BranchStatus,
    EdgeKind,
    Environment,
    FailureKind,
    IterationEdge,
    IterationNode,
    IterationPlan,
    NodeStatus,
    RunConfig,
)
from .storage import RunStore, json_text


def patch_identity(base_revision: str, patch_bytes: bytes) -> str:
    # Git diff bytes are preserved verbatim. Normalizing trailing newlines avoids
    # meaningless copies without parsing or rewriting kernel code.
    normalized = patch_bytes.rstrip(b"\r\n") + (b"\n" if patch_bytes else b"")
    return hashlib.sha256(base_revision.encode() + b"\0" + normalized).hexdigest()


class IterationMap:
    def __init__(self, store: RunStore):
        self.store = store

    def initialize(self, config: RunConfig) -> IterationNode:
        self.store.initialize()
        baseline_id = f"{config.operator}_0000"
        baseline = IterationNode(
            node_id=baseline_id,
            operator=config.operator,
            base_revision=config.base_revision,
            candidate_revision=config.base_revision,
            patch_id=patch_identity(config.base_revision, b""),
            branch="baseline",
            generation=0,
            status=NodeStatus.ACCEPTED,
            branch_status=BranchStatus.CHAMPION,
            tags=["baseline", "simulated"] if config.mode == "simulated" else ["baseline"],
        )
        self.store.create_node(baseline)
        self.store.set_champion(config.operator, baseline_id)
        return baseline

    def next_id(self, operator: str) -> str:
        used = [int(node.node_id.rsplit("_", 1)[1]) for node in self.store.nodes(operator)]
        return f"{operator}_{max(used, default=-1) + 1:04d}"

    def create_candidate(
        self,
        config: RunConfig,
        plan: IterationPlan,
        *,
        patch_id: str = "pending",
        patch_path: str | None = None,
        parent_patch_paths: Iterable[str] | None = None,
        base_revision: str | None = None,
        parent_ids: Iterable[str] | None = None,
        ablates_change_id: str | None = None,
        allow_empty_changes: bool = False,
    ) -> IterationNode:
        parent_ids = list(parent_ids or [plan.parent_node_id or self.require_champion(config.operator)])
        parents = [self.store.load_node(node_id) for node_id in parent_ids]
        if len(parent_ids) > 1 and not ablates_change_id:
            raise NotImplementedError("Multi-parent composition requires an explicit reviewed adapter")
        if len(set(parent_ids)) != len(parent_ids):
            raise ValueError("Duplicate parents are not allowed")
        if any(parent.operator != config.operator for parent in parents):
            raise ValueError("Cannot create a cross-operator candidate")
        for workload_id in plan.target_workloads:
            if workload_id not in {item.workload_id for item in config.workloads}:
                raise ValueError(f"Unknown public workload: {workload_id}")
        change_ids = [item.change_id for item in plan.semantic_changes]
        if not change_ids and not allow_empty_changes:
            raise ValueError("Candidate must declare at least one semantic change")
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("Semantic change IDs must be unique")
        for change in plan.semantic_changes:
            if unknown := set(change.depends_on) - set(change_ids):
                raise ValueError(f"Unknown semantic change dependencies: {unknown}")
            for filename in change.files:
                if filename not in config.allowed_files:
                    raise ValueError(f"Semantic change touches forbidden file: {filename}")
        parent_patch_paths = list(
            [parents[0].patch_path] if parents[0].patch_path
            else [parent.patch_path for parent in parents if parent.patch_path]
        ) if parent_patch_paths is None else list(parent_patch_paths)
        for parent_patch in parent_patch_paths:
            path = self.store.artifacts / parent_patch
            if not path.is_file():
                raise ValueError(f"Parent patch is missing: {parent_patch}")
            patch_name = parent_patch.rsplit("/", 1)[-1]
            expected_hash = patch_name.rsplit("-", 1)[-1].removesuffix(".patch")
            if patch_identity(base_revision or parents[0].base_revision, path.read_bytes()) != expected_hash:
                raise ValueError("Parent patch identity does not match its content")
        node_id = self.next_id(config.operator)
        node = IterationNode(
            node_id=node_id,
            workspace=f"workspaces/{node_id}",
            operator=config.operator,
            parent_ids=parent_ids,
            base_revision=base_revision or parents[0].base_revision,
            candidate_revision="pending",
            patch_id=patch_id,
            patch_path=patch_path,
            parent_patch_paths=parent_patch_paths,
            branch=f"ablate-{ablates_change_id}" if ablates_change_id else f"branch-{change_ids[0]}",
            generation=max(parent.generation for parent in parents) + 1,
            hypothesis=plan.hypothesis,
            semantic_changes=plan.semantic_changes,
            target_workloads=plan.target_workloads,
            status=NodeStatus.PLANNED,
            ablates_change_id=ablates_change_id,
        )
        self.store.create_node(node)
        edges = []
        for index, parent_id in enumerate(parent_ids):
            kind = EdgeKind.DERIVED_FROM if index == 0 else EdgeKind.COMBINES
            edges.append(IterationEdge(kind=kind, from_node=node.node_id, to_node=parent_id))
        if ablates_change_id:
            edges.append(IterationEdge(
                kind=EdgeKind.ABLATES, from_node=node.node_id, to_node=parent_ids[0],
                metadata={"change_id": ablates_change_id},
            ))
        self.store.add_edges(edges)
        return node

    def capture_patch(
        self,
        node: IterationNode,
        patch_bytes: bytes,
        *,
        candidate_revision: str,
        allow_empty: bool = False,
    ) -> IterationNode:
        if not patch_bytes and not allow_empty:
            raise ValueError("Candidate made no changes")
        if node.patch_id != "pending":
            raise ValueError("A persisted candidate patch is immutable")
        patch_id = patch_identity(node.base_revision, patch_bytes)
        duplicates = [
            other for other in self.store.nodes(node.operator)
            if other.patch_id == patch_id and other.node_id != node.node_id
            and not (allow_empty and other.generation == 0)
        ]
        if duplicates:
            raise ValueError(f"Identical candidate already exists: {duplicates[0].node_id}")
        relative = f"patches/{node.node_id}-{patch_id}.patch" if allow_empty else f"patches/{patch_id}.patch"
        destination = self.store.artifacts / relative
        if destination.exists() and destination.read_bytes() != patch_bytes:
            raise ValueError("Patch content hash collision")
        if not destination.exists():
            from .storage import atomic_write
            atomic_write(destination, patch_bytes.decode("utf-8"), overwrite=False)
        updated = node.model_copy(update={
            "patch_id": patch_id, "patch_path": relative, "candidate_revision": candidate_revision,
        })
        # Materialization is the single permitted revision of a planned node's
        # code identity; all later calls use the normal immutable-field check.
        self.store.save_node(updated)
        self.store.event("PatchCaptured", node.node_id, {"patch_id": patch_id})
        return updated

    def transition(
        self,
        node: IterationNode,
        status: NodeStatus,
        *,
        failure_kind: FailureKind | None = None,
        reason: str | None = None,
        evaluation_id: str | None = None,
    ) -> IterationNode:
        current = self.store.load_node(node.node_id)
        evaluation_ids = list(current.evaluation_ids)
        if evaluation_id and evaluation_id not in evaluation_ids:
            evaluation_ids.append(evaluation_id)
        updated = current.model_copy(update={
            "status": status,
            "failure_kind": failure_kind,
            "failure_reason": reason,
            "evaluation_ids": evaluation_ids,
        })
        self.store.save_node(updated, previous=current)
        self.store.event("NodeStatusChanged", node.node_id, {
            "from": current.status, "to": status, "reason": reason,
        })
        return updated

    def promote(self, node: IterationNode, evaluation_id: str) -> IterationNode:
        evaluation = next((item for item in self.store.evaluations(node.node_id)
                           if item.evaluation_id == evaluation_id), None)
        if node.ablates_change_id:
            raise ValueError("Ablation branches cannot become Champion")
        if evaluation is None or evaluation.verdict != "ACCEPT":
            raise ValueError("Only an ACCEPT evaluation can promote a candidate")
        if evaluation.baseline_node_id != self.require_champion(node.operator):
            raise ValueError("Candidate was not evaluated against the current Champion")
        previous_champion = self.store.load_node(evaluation.baseline_node_id)
        updated = self.transition(node, NodeStatus.ACCEPTED, evaluation_id=evaluation_id)
        self.store.save_node(previous_champion.model_copy(update={"branch_status": BranchStatus.MERGED}),
                             previous=previous_champion)
        current = self.store.load_node(node.node_id)
        self.store.save_node(current.model_copy(update={"branch_status": BranchStatus.CHAMPION}), previous=current)
        self.store.set_champion(node.operator, node.node_id)
        self.store.add_edges([IterationEdge(
            kind=EdgeKind.SUPERSEDES, from_node=node.node_id, to_node=previous_champion.node_id,
            metadata={"evaluation_id": evaluation_id},
        )])
        return self.store.load_node(node.node_id)

    def require_champion(self, operator: str) -> str:
        champion = self.store.champion(operator)
        if champion is None:
            raise ValueError(f"No Champion for {operator}")
        return champion

    def parents(self, node: IterationNode) -> list[IterationNode]:
        return [self.store.load_node(parent) for parent in node.parent_ids]

    def path(self, node_id: str) -> list[IterationNode]:
        node = self.store.load_node(node_id)
        if not node.parent_ids:
            return [node]
        if len(node.parent_ids) > 1:
            raise ValueError("A combination node has multiple parent paths; inspect its edges")
        return self.path(node.parent_ids[0]) + [node]

    def primary_path(self, node_id: str) -> list[IterationNode]:
        """Follow the materialization parent, retaining other edges separately."""
        node = self.store.load_node(node_id)
        if not node.parent_ids:
            return [node]
        return self.primary_path(node.parent_ids[0]) + [node]

    def assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError(f"Cycle detected at {node_id}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for parent in self.store.load_node(node_id).parent_ids:
                visit(parent)
            visiting.remove(node_id)
            visited.add(node_id)

        for node in self.store.nodes():
            visit(node.node_id)

    def invalidate_stale(self, environment: Environment, protocol: str, operator: str) -> list[str]:
        stale = []
        for node in self.store.nodes(operator):
            if node.generation == 0 or node.status not in {NodeStatus.ACCEPTED, NodeStatus.REJECTED}:
                continue
            evaluations = self.store.evaluations(node.node_id)
            if not evaluations or any(
                item.environment != environment or item.protocol_version != protocol for item in evaluations
            ):
                self.transition(node, NodeStatus.NEEDS_REVALIDATION,
                                reason="Environment or evaluation protocol changed")
                stale.append(node.node_id)
        return stale

    def render(self, operator: str) -> str:
        nodes = {node.node_id: node for node in self.store.nodes(operator)}
        children: dict[str | None, list[str]] = {}
        for node in nodes.values():
            children.setdefault(node.parent_ids[0] if node.parent_ids else None, []).append(node.node_id)
        champion = self.store.champion(operator)
        lines = [operator]

        def visit(node_id: str, prefix: str = "") -> None:
            node = nodes[node_id]
            status = "CHAMPION" if node_id == champion else node.status.value
            suffix = f" [{status}]"
            if node.failure_reason:
                suffix += f": {node.failure_reason}"
            lines.append(f"{prefix}{node.node_id} {node.hypothesis or 'baseline'}{suffix}")
            if len(node.parent_ids) > 1:
                lines.append(f"{prefix}  combines: {', '.join(node.parent_ids[1:])}")
            for child in sorted(children.get(node_id, [])):
                visit(child, prefix + "  ")

        for root in children.get(None, []):
            visit(root)
        return "\n".join(lines)

    def summary(self, operator: str, *, limit: int = 12) -> dict[str, object]:
        nodes = self.store.nodes(operator)
        champion = self.require_champion(operator)
        return {
            "operator": operator,
            "champion_node": champion,
            "best_path": [node.node_id for node in self.primary_path(champion)],
            "active_branches": [node.node_id for node in nodes if node.branch_status == BranchStatus.ACTIVE],
            "failed_directions": [{
                "node_id": node.node_id, "hypothesis": node.hypothesis,
                "failure_kind": node.failure_kind, "reason": node.failure_reason,
            } for node in nodes if node.status == NodeStatus.REJECTED][-limit:],
            "pending_ablations": [node.node_id for node in nodes
                                  if node.status == NodeStatus.ACCEPTED and len(node.semantic_changes) > 1],
            "revalidation_required": [node.node_id for node in nodes
                                      if node.status == NodeStatus.NEEDS_REVALIDATION],
            "node_count": len(nodes),
        }

    def diff(self, left_id: str, right_id: str) -> str:
        left = self.store.load_node(left_id)
        right = self.store.load_node(right_id)
        left_patch = (self.store.artifacts / left.patch_path).read_text() if left.patch_path else ""
        right_patch = (self.store.artifacts / right.patch_path).read_text() if right.patch_path else ""
        return json_text({
            "left": left, "right": right,
            "left_parent_path": [node.node_id for node in self.primary_path(left_id)],
            "right_parent_path": [node.node_id for node in self.primary_path(right_id)],
            "left_patch": left_patch, "right_patch": right_patch,
        })
