from __future__ import annotations

from pathlib import Path

import pytest

from flaggems_rsi.map import IterationMap, patch_identity
from flaggems_rsi.schemas import BranchStatus, NodeStatus
from flaggems_rsi.storage import RunStore


def test_lineage_supports_historical_branches_and_multi_parent(config, plan):
    store = RunStore(config.repository.parent / "run")
    lineage = IterationMap(store)
    lineage.initialize(config)
    first = lineage.create_candidate(config, plan)
    first = lineage.capture_patch(first, b"diff --git a/kernel.py b/kernel.py\n", candidate_revision="rev1")
    branch_plan = plan.model_copy(update={"hypothesis": "A different historical branch"})
    second = lineage.create_candidate(config, branch_plan, parent_ids=["sim_copy_0000"])
    assert second.parent_ids == ["sim_copy_0000"]
    with pytest.raises(NotImplementedError, match="composition"):
        lineage.create_candidate(config, branch_plan, parent_ids=[first.node_id, second.node_id])
    ablation_plan = branch_plan.model_copy(update={"parent_node_id": first.node_id})
    combination = lineage.create_candidate(config, ablation_plan, parent_ids=[first.node_id, second.node_id],
                                          ablates_change_id="work")
    assert combination.generation == 2
    assert {edge.kind for edge in store.edges()} >= {"DERIVED_FROM", "COMBINES"}
    lineage.assert_acyclic()
    assert [item.node_id for item in lineage.path(first.node_id)] == ["sim_copy_0000", first.node_id]
    assert lineage.store.champion("sim_copy") == "sim_copy_0000"


def test_patch_content_is_immutable_and_deduplicated(config, plan):
    store = RunStore(config.repository.parent / "run")
    lineage = IterationMap(store)
    lineage.initialize(config)
    node = lineage.create_candidate(config, plan)
    patch = b"diff --git a/kernel.py b/kernel.py\n"
    node = lineage.capture_patch(node, patch, candidate_revision="rev1")
    with pytest.raises(ValueError, match="immutable"):
        lineage.capture_patch(node, patch, candidate_revision="rev2")
    duplicate = lineage.create_candidate(config, plan)
    with pytest.raises(ValueError, match="Identical candidate"):
        lineage.capture_patch(duplicate, patch, candidate_revision="rev2")
    with pytest.raises(FileExistsError):
        store.create_node(node)


def test_path_detects_corrupt_cycle(config):
    store = RunStore(config.repository.parent / "run")
    lineage = IterationMap(store)
    lineage.initialize(config)
    baseline = store.load_node("sim_copy_0000")
    corrupt = baseline.model_copy(update={"parent_ids": ["sim_copy_0000"]})
    store.save_node(corrupt)
    with pytest.raises(ValueError, match="Cycle"):
        lineage.assert_acyclic()
