"""Project immutable run facts into a SQLite knowledge base.

Reads only:
    <runs>/<run>/config.json
    <runs>/<run>/map/nodes/*.json
    <runs>/<run>/map/edges.jsonl
    <runs>/<run>/map/evaluations/*.json
    <profiles>/*.json                  (optional DeviceProfile files)

Writes only:
    the SQLite database.

Nothing here mutates a run directory. `rebuild` is idempotent: derived tables are
dropped and recreated, the technique vocabulary is preserved and extended.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"
SEED = HERE / "techniques.json"

SCHEMA_VERSION = "1"

# Software keys that define a lane. Deliberately excludes repository/revision:
# those change per node and would shatter every lane.
STACK_KEYS = ("torch", "triton", "flag_gems", "python")


# --------------------------------------------------------------------------- io


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def iter_runs(root: Path) -> list[Path]:
    """A run directory is a directory with config.json; otherwise scan children."""
    root = Path(root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"runs path does not exist: {root}")
    if (root / "config.json").is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "config.json").is_file())


def load_profiles(paths: Iterable[str | Path]) -> dict[str, dict[str, Any]]:
    """Index DeviceProfile-shaped JSON files by profile_id.

    Any JSON object with both `profile_id` and `device_name` is accepted; run
    configs and other files are skipped silently.
    """
    profiles: dict[str, dict[str, Any]] = {}
    for raw in paths:
        path = Path(raw).resolve()
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file in files:
            data = _read_json(file)
            if data and data.get("profile_id") and data.get("device_name"):
                profiles[str(data["profile_id"])] = data
    return profiles


# ------------------------------------------------------------------------ lanes


def _workload_fingerprint(config: dict[str, Any]) -> tuple[str, str]:
    workloads = config.get("workloads") or []
    dtypes = sorted({str(w.get("dtype") or "float32") for w in workloads})
    shapes: list[str] = []
    for workload in workloads:
        for shape in workload.get("shapes") or []:
            dims = "x".join(str(d) for d in shape) if isinstance(shape, list) else str(shape)
            shapes.append(f"{workload.get('workload_id', '?')}={dims}")
    return ",".join(dtypes) or "unknown", ",".join(sorted(shapes)) or "unknown"


def _stack_text(profile: dict[str, Any]) -> str:
    software = profile.get("software") or {}
    return _json({k: software[k] for k in STACK_KEYS if k in software})


def _lane_id(**parts: str) -> str:
    raw = "\x1f".join(parts[key] for key in sorted(parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _resolve_profile(
    node: dict[str, Any], operator: str, profiles: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    profile_id = node.get("device_profile_id")
    if profile_id and profile_id in profiles:
        return profiles[profile_id]
    base = node.get("base_revision")
    for profile in profiles.values():
        if profile.get("operator") not in (None, operator):
            continue
        revision = (profile.get("software") or {}).get("revision")
        if base and revision == base:
            return profile
    for profile in profiles.values():
        if profile.get("operator") == operator:
            return profile
    return {}


# ------------------------------------------------------------------------ build


def _insert_lane(conn: sqlite3.Connection, lane: dict[str, str]) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO lane(lane_id, operator, dtype, shape_bucket, vendor, device,"
        " stack_json, protocol_version) VALUES (?,?,?,?,?,?,?,?)",
        (
            lane["lane_id"], lane["operator"], lane["dtype"], lane["shape_bucket"],
            lane["vendor"], lane["device"], lane["stack_json"], lane["protocol_version"],
        ),
    )
    return lane["lane_id"]


def _seed_techniques(conn: sqlite3.Connection) -> int:
    if not SEED.is_file():
        return 0
    try:
        entries = json.loads(SEED.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    added = 0
    for entry in entries:
        technique_id = entry.get("technique_id")
        if not technique_id:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO technique(technique_id, family, kind, description,"
            " aliases_json, status, merged_into, applies_when_json, origin)"
            " VALUES (?,?,?,?,?,?,NULL,?, 'seed')",
            (
                technique_id,
                entry.get("family") or "other",
                entry.get("kind"),
                entry.get("description"),
                _json(entry.get("aliases") or []),
                "stable",
                _json(entry.get("applies_when") or {}),
            ),
        )
        added += cur.rowcount
    return added


def _insert_draft(conn: sqlite3.Connection, technique_id: str, family: str, description: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO technique(technique_id, family, kind, description,"
        " aliases_json, status, merged_into, applies_when_json, origin)"
        " VALUES (?,?,?,?,'[]','draft',NULL,'{}','draft')",
        (technique_id, family, family, description),
    )


def rebuild(
    db_path: str | Path,
    runs_root: str | Path,
    profile_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Drop derived tables, re-ingest every run, preserve the vocabulary."""
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    runs = iter_runs(Path(runs_root))
    profiles = load_profiles(profile_paths)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        seeded = _seed_techniques(conn)

        stats = {"runs": [], "nodes": 0, "edges": 0, "changes": 0,
                 "evaluations": 0, "measurements": 0, "lanes": 0, "techniques_seeded": seeded}
        seen_lanes: set[str] = set()
        drafts: dict[str, tuple[str, str]] = {}

        for run in runs:
            config = _read_json(run / "config.json") or {}
            run_id = config.get("run_id") or run.name
            operator = config.get("operator") or "unknown"
            protocol = (config.get("evaluation") or {}).get("protocol_version") or "unknown"
            dtype, shape_bucket = _workload_fingerprint(config)
            repository = str(config.get("repository") or "")
            repo_name = Path(repository).name if repository else run_id

            map_dir = run / "map"
            nodes = [n for n in (_read_json(p) for p in sorted((map_dir / "nodes").glob("*.json"))) if n]

            for node in nodes:
                node_operator = node.get("operator") or operator
                profile = _resolve_profile(node, node_operator, profiles)
                lane = {
                    "lane_id": _lane_id(
                        operator=node_operator,
                        dtype=dtype,
                        shape_bucket=shape_bucket,
                        vendor=str(profile.get("vendor") or "unknown"),
                        device=str(profile.get("device_name") or "unknown"),
                        stack=_stack_text(profile),
                        protocol_version=protocol,
                    ),
                    "operator": node_operator,
                    "dtype": dtype,
                    "shape_bucket": shape_bucket,
                    "vendor": str(profile.get("vendor") or "unknown"),
                    "device": str(profile.get("device_name") or "unknown"),
                    "stack_json": _stack_text(profile),
                    "protocol_version": protocol,
                }
                _insert_lane(conn, lane)
                seen_lanes.add(lane["lane_id"])

                tree_sha = node.get("tree_sha") or ""
                candidate = str(node.get("candidate_revision") or "")
                if tree_sha:
                    uid = f"{repo_name}#{tree_sha}"
                elif candidate and candidate != "pending":
                    uid = f"{repo_name}#{candidate}"
                else:
                    # Never materialised into code; keep a distinct placeholder
                    # instead of letting every pending node collide on one id.
                    uid = f"{repo_name}#unmaterialized:{node.get('node_id')}"
                patch_sha = str(node.get("patch_id") or "")
                applied = patch_sha not in ("", "pending") and bool(node.get("patch_path"))

                conn.execute(
                    "INSERT OR REPLACE INTO node(node_id, uid, lane_id, operator, run_id,"
                    " generation, base_commit, candidate_revision, tree_sha, patch_sha, patch_path,"
                    " hypothesis, bottleneck_class, status, failure_kind, failure_reason,"
                    " parent_ids_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node.get("node_id"), uid, lane["lane_id"], node_operator, run_id,
                        node.get("generation", 0), node.get("base_revision"), candidate,
                        tree_sha or None, patch_sha or None, node.get("patch_path"),
                        node.get("hypothesis") or "", node.get("bottleneck_class") or "unknown",
                        node.get("status"), node.get("failure_kind"), node.get("failure_reason"),
                        _json(node.get("parent_ids") or []), node.get("created_at"),
                    ),
                )
                stats["nodes"] += 1

                for change in node.get("semantic_changes") or []:
                    change_id = str(change.get("change_id") or "unnamed")
                    kind = str(change.get("kind") or "other")
                    technique_id = change.get("technique_id") or f"{kind}.{change_id}"
                    params = {
                        key: change[key]
                        for key in ("operation", "value", "expected_effect", "ablation", "depends_on")
                        if change.get(key) not in (None, "", [], {})
                    }
                    conn.execute(
                        "INSERT OR IGNORE INTO change(node_id, technique_id, params_json, role,"
                        " files_json, note) VALUES (?,?,?,?,?,?)",
                        (
                            node.get("node_id"), technique_id, _json(params),
                            "APPLIED" if applied else "CONSIDERED",
                            _json(change.get("files") or []),
                            change.get("description") or "",
                        ),
                    )
                    drafts.setdefault(
                        technique_id, (kind, str(change.get("description") or ""))
                    )
                    stats["changes"] += 1

            for edge in _read_jsonl(map_dir / "edges.jsonl"):
                src, dst, kind = edge.get("from_node"), edge.get("to_node"), edge.get("kind")
                if not (src and dst and kind):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO edge(src, dst, type) VALUES (?,?,?)",
                    (src, dst, kind),
                )
                stats["edges"] += 1

            for path in sorted((map_dir / "evaluations").glob("*.json")):
                ev = _read_json(path)
                if not ev:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO evaluation(evaluation_id, node_id, baseline_node_id,"
                    " protocol_version, env_json, correctness, correctness_cases,"
                    " correctness_failures_json, verdict, reasons_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ev.get("evaluation_id"), ev.get("node_id"), ev.get("baseline_node_id"),
                        ev.get("protocol_version"), _json(ev.get("environment") or {}),
                        ev.get("correctness"), ev.get("correctness_cases", 0),
                        _json(ev.get("correctness_failures") or []),
                        ev.get("verdict"), _json(ev.get("reasons") or []),
                        ev.get("created_at"),
                    ),
                )
                stats["evaluations"] += 1
                for workload in ev.get("workloads") or []:
                    conn.execute(
                        "INSERT OR REPLACE INTO measurement(evaluation_id, node_id, workload_id,"
                        " role, median_ms, baseline_median_ms, speedup, noise, samples_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            ev.get("evaluation_id"), ev.get("node_id"),
                            workload.get("workload_id"), workload.get("role"),
                            workload.get("candidate_median_ms"), workload.get("baseline_median_ms"),
                            workload.get("speedup"), workload.get("noise"),
                            _json(workload.get("measurements") or []),
                        ),
                    )
                    stats["measurements"] += 1

            stats["runs"].append({"run_id": run_id, "nodes": len(nodes)})

        for technique_id, (family, description) in drafts.items():
            _insert_draft(conn, technique_id, family, description)

        stats["lanes"] = len(seen_lanes)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_runs', ?)",
            (_json([str(r) for r in runs]),),
        )
        conn.commit()
        stats["db"] = str(db_path)
        return stats
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    import sys

    print(json.dumps(rebuild(sys.argv[1] if len(sys.argv) > 1 else HERE / "kb.db",
                             sys.argv[2] if len(sys.argv) > 2 else HERE.parent / "runs"),
                     indent=2))
