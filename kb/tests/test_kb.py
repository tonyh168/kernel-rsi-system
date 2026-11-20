"""Self-contained tests for kernel-kb.

Run from the repository root:

    python3 -m pytest kb/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

KB_DIR = Path(__file__).resolve().parents[1]
if str(KB_DIR) not in sys.path:
    sys.path.insert(0, str(KB_DIR))

import digest  # noqa: E402
import project  # noqa: E402


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


@pytest.fixture()
def demo_run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "demo"
    _write(
        run / "config.json",
        {
            "run_id": "demo",
            "mode": "simulated",
            "operator": "demo_op",
            "repository": str(tmp_path / "repo"),
            "workloads": [
                {"workload_id": "small", "shapes": [[64]], "role": "target"},
                {"workload_id": "large", "shapes": [[256]], "role": "guard"},
            ],
            "evaluation": {"protocol_version": "test-v1"},
        },
    )
    _write(
        run / "map" / "nodes" / "n_0000.json",
        {
            "node_id": "n_0000",
            "operator": "demo_op",
            "generation": 0,
            "base_revision": "aaa",
            "candidate_revision": "aaa",
            "patch_id": "base",
            "patch_path": None,
            "parent_ids": [],
            "hypothesis": "",
            "semantic_changes": [],
            "status": "ACCEPTED",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    _write(
        run / "map" / "nodes" / "n_0001.json",
        {
            "node_id": "n_0001",
            "operator": "demo_op",
            "generation": 1,
            "base_revision": "aaa",
            "candidate_revision": "bbb",
            "patch_id": "bbb",
            "patch_path": "patches/bbb.patch",
            "parent_ids": ["n_0000"],
            "hypothesis": "bigger tile",
            "semantic_changes": [
                {
                    "change_id": "tile",
                    "kind": "launch_config",
                    "description": "grow BLOCK_M",
                    "files": ["k.py"],
                }
            ],
            "status": "ACCEPTED",
            "created_at": "2026-01-02T00:00:00Z",
        },
    )
    _write(
        run / "map" / "nodes" / "n_0002.json",
        {
            "node_id": "n_0002",
            "operator": "demo_op",
            "generation": 2,
            "base_revision": "aaa",
            "candidate_revision": "pending",
            "patch_id": "pending",
            "patch_path": None,
            "parent_ids": ["n_0001"],
            "hypothesis": "drop the mask",
            "semantic_changes": [
                {"change_id": "skip-mask", "kind": "algorithm", "description": "skip mask"}
            ],
            "status": "REJECTED",
            "failure_kind": "IMPLEMENTATION_FAILED",
            "failure_reason": "correctness FAIL",
            "created_at": "2026-01-03T00:00:00Z",
        },
    )
    edges = [
        {"from_node": "n_0001", "to_node": "n_0000", "kind": "DERIVED_FROM"},
        {"from_node": "n_0001", "to_node": "n_0000", "kind": "SUPERSEDES"},
        {"from_node": "n_0002", "to_node": "n_0001", "kind": "DERIVED_FROM"},
    ]
    (run / "map").mkdir(parents=True, exist_ok=True)
    (run / "map" / "edges.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )
    _write(
        run / "map" / "evaluations" / "eval_1.json",
        {
            "evaluation_id": "eval_1",
            "node_id": "n_0001",
            "baseline_node_id": "n_0000",
            "protocol_version": "test-v1",
            "environment": {"backend": "simulated", "python": "3.12", "platform": "test",
                            "fixture_protocol": "test-v1", "details": {}},
            "correctness": "PASS",
            "correctness_cases": 4,
            "correctness_failures": [],
            "workloads": [
                {"workload_id": "small", "role": "target",
                 "measurements": [{"candidate_ms": 8.0, "baseline_ms": 10.0}],
                 "candidate_median_ms": 8.0, "baseline_median_ms": 10.0,
                 "speedup": 1.25, "noise": 0.01},
                {"workload_id": "large", "role": "guard",
                 "measurements": [{"candidate_ms": 10.2, "baseline_ms": 10.0}],
                 "candidate_median_ms": 10.2, "baseline_median_ms": 10.0,
                 "speedup": 0.98, "noise": 0.01},
            ],
            "verdict": "ACCEPT",
            "reasons": [],
            "created_at": "2026-01-02T00:00:00Z",
        },
    )
    return tmp_path / "runs"


def test_rebuild_populates_facts(tmp_path: Path, demo_run: Path) -> None:
    db = tmp_path / "kb.db"
    stats = project.rebuild(db, demo_run)
    assert stats["nodes"] == 3
    assert stats["edges"] == 3
    assert stats["evaluations"] == 1
    assert stats["measurements"] == 2

    conn = project.sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM lane").fetchone()[0] == 1
        # patch was captured -> APPLIED; never captured -> CONSIDERED
        roles = dict(conn.execute("SELECT technique_id, role FROM change").fetchall())
        assert roles["launch_config.tile"] == "APPLIED"
        assert roles["algorithm.skip-mask"] == "CONSIDERED"
        # auto-derived draft vocabulary
        status = dict(conn.execute("SELECT technique_id, status FROM technique").fetchall())
        assert status["launch_config.tile"] == "draft"
    finally:
        conn.close()


def test_rebuild_is_idempotent_and_preserves_vocabulary(tmp_path: Path, demo_run: Path) -> None:
    db = tmp_path / "kb.db"
    project.rebuild(db, demo_run)

    conn = project.sqlite3.connect(db)
    conn.execute("UPDATE technique SET status = 'stable', merged_into = NULL"
                 " WHERE technique_id = 'launch_config.tile'")
    conn.commit()
    conn.close()

    project.rebuild(db, demo_run)
    conn = project.sqlite3.connect(db)
    try:
        status = conn.execute(
            "SELECT status FROM technique WHERE technique_id = 'launch_config.tile'"
        ).fetchone()[0]
        assert status == "stable"          # manual curation survives rebuild
        assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 3  # no duplicates
    finally:
        conn.close()


def test_digest_reports_champion_techniques_dead_ends(tmp_path: Path, demo_run: Path) -> None:
    db = tmp_path / "kb.db"
    project.rebuild(db, demo_run)
    conn = project.sqlite3.connect(db)
    try:
        text = digest.render(conn, "demo_op", minimum_samples=1)
    finally:
        conn.close()

    assert "champion `n_0001`" in text
    assert "launch_config.tile" in text
    assert "n_0002" in text and "correctness FAIL" in text
    assert "path: n_0000 → n_0001" in text


def test_card_exposes_evidence(tmp_path: Path, demo_run: Path) -> None:
    db = tmp_path / "kb.db"
    project.rebuild(db, demo_run)
    conn = project.sqlite3.connect(db)
    try:
        data = digest.card(conn, "n_0001")
    finally:
        conn.close()
    assert data is not None
    assert data["parents"] == ["n_0000"]
    assert data["changes"][0]["technique_id"] == "launch_config.tile"
    assert len(data["measurements"]) == 2
    assert data["evaluations"][0]["verdict"] == "ACCEPT"


def test_unknown_operator_returns_hint(tmp_path: Path, demo_run: Path) -> None:
    db = tmp_path / "kb.db"
    project.rebuild(db, demo_run)
    conn = project.sqlite3.connect(db)
    try:
        text = digest.render(conn, "does_not_exist")
    finally:
        conn.close()
    assert "No lane recorded" in text
