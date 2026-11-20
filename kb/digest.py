"""Render the agent-facing digest.

This module is the reason the knowledge base exists. It answers, in one bounded
markdown block, four questions an agent asks before its next iteration:

    1. where did this operator get to
    2. which techniques actually worked here
    3. which directions are dead ends
    4. what worked elsewhere and has not been tried here

Everything is derived by SQL. No model involvement, no free-text summarization.
"""

from __future__ import annotations

import sqlite3
from typing import Any

FAILED_STATUSES = ("REJECTED", "INVALID", "PARKED")


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _champion(conn: sqlite3.Connection, lane_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT node_id, generation, best_speedup FROM v_champion"
        " WHERE lane_id = ? ORDER BY generation DESC, created_at DESC LIMIT 1",
        (lane_id,),
    ).fetchone()
    return dict(row) if row else None


def _path(conn: sqlite3.Connection, node_id: str, limit: int = 32) -> list[str]:
    chain = [node_id]
    current = node_id
    for _ in range(limit):
        row = conn.execute(
            "SELECT dst FROM edge WHERE src = ? AND type = 'DERIVED_FROM' LIMIT 1", (current,)
        ).fetchone()
        if not row or row[0] in chain:
            break
        current = row[0]
        chain.append(current)
    return list(reversed(chain))


def _techniques(conn: sqlite3.Connection, lane_id: str, minimum: int = 2) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT technique_id, n, win_rate, avg_speedup FROM v_tech_lift"
        " WHERE lane_id = ? AND n >= ? ORDER BY win_rate DESC, avg_speedup DESC LIMIT 8",
        (lane_id, minimum),
    ).fetchall()
    return [dict(row) for row in rows]


def _dead_ends(conn: sqlite3.Connection, lane_id: str, limit: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in FAILED_STATUSES)
    rows = conn.execute(
        f"SELECT node_id, hypothesis, status, failure_kind, failure_reason FROM node"
        f" WHERE lane_id = ? AND status IN ({placeholders})"
        f" ORDER BY created_at DESC LIMIT ?",
        (lane_id, *FAILED_STATUSES, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _imports(conn: sqlite3.Connection, lane_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.technique_id, l.vendor, l.device, v.win_rate, v.avg_speedup, v.n"
        " FROM v_tech_lift v JOIN lane l ON l.lane_id = v.lane_id"
        " WHERE v.lane_id <> ? AND v.win_rate >= 0.5"
        "   AND v.technique_id NOT IN (SELECT technique_id FROM v_tech_lift WHERE lane_id = ?)"
        " ORDER BY v.win_rate DESC, v.avg_speedup DESC LIMIT ?",
        (lane_id, lane_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def render(
    conn: sqlite3.Connection,
    operator: str,
    *,
    vendor: str | None = None,
    failures: int = 8,
    minimum_samples: int = 2,
) -> str:
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM lane WHERE operator = ?"
    params: list[Any] = [operator]
    if vendor:
        sql += " AND vendor = ?"
        params.append(vendor)
    lanes = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if not lanes:
        return f"# {operator}\n\nNo lane recorded. Run `rebuild` first, or check the operator name.\n"

    lanes.sort(
        key=lambda lane: conn.execute(
            "SELECT COUNT(*) FROM node WHERE lane_id = ?", (lane["lane_id"],)
        ).fetchone()[0],
        reverse=True,
    )
    lane = lanes[0]
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node WHERE lane_id = ?", (lane["lane_id"],)
    ).fetchone()[0]

    lines: list[str] = []
    lines.append(
        f"## {operator} @ {lane['vendor']}/{lane['device']} · {lane['dtype']}"
        f" · {lane['protocol_version']}"
    )
    lines.append(f"lane `{lane['lane_id']}` · {node_count} nodes")

    champion = _champion(conn, lane["lane_id"])
    if champion:
        lines.append(
            f"champion `{champion['node_id']}` {_fmt(champion['best_speedup'], 2, 'x')}"
        )
        lines.append("path: " + " → ".join(_path(conn, champion["node_id"])))
    else:
        lines.append("champion: none yet")

    lines.append("")
    lines.append(f"### 本 lane 已验证手段 (n≥{minimum_samples})")
    techniques = _techniques(conn, lane["lane_id"], minimum_samples)
    if techniques:
        lines.append("| technique | n | win | avg |")
        lines.append("|---|---|---|---|")
        for item in techniques:
            lines.append(
                f"| {item['technique_id']} | {item['n']} |"
                f" {_fmt(item['win_rate'], 2)} | {_fmt(item['avg_speedup'], 2, 'x')} |"
            )
    else:
        lines.append("(暂无：还没有带评测的节点)")

    lines.append("")
    lines.append(f"### 已否方向（最近 {failures}）")
    dead = _dead_ends(conn, lane["lane_id"], failures)
    if dead:
        for item in dead:
            reason = item["failure_reason"] or item["failure_kind"] or item["status"]
            lines.append(
                f"- `{item['node_id']}` {item['status']}：{item['hypothesis'] or '—'} → {reason}"
            )
    else:
        lines.append("(无)")

    lines.append("")
    lines.append("### 跨 lane 候选（别处有效、本 lane 未试）")
    imports = _imports(conn, lane["lane_id"])
    if imports:
        for item in imports:
            lines.append(
                f"- `{item['technique_id']}` — {item['vendor']}/{item['device']}:"
                f" win {_fmt(item['win_rate'], 2)} / avg {_fmt(item['avg_speedup'], 2, 'x')}"
                f" (n={item['n']})"
            )
    else:
        lines.append("(无)")

    if len(lanes) > 1:
        lines.append("")
        lines.append("### 其他可比域")
        for other in lanes[1:]:
            lines.append(
                f"- `{other['lane_id']}` {other['vendor']}/{other['device']} · {other['dtype']}"
            )

    lines.append("")
    return "\n".join(lines)


def card(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    """Full context for one node, as a JSON-serialisable dict."""
    conn.row_factory = sqlite3.Row
    node = conn.execute("SELECT * FROM node WHERE node_id = ?", (node_id,)).fetchone()
    if not node:
        return None
    result = dict(node)
    result["changes"] = [
        dict(row)
        for row in conn.execute(
            "SELECT technique_id, role, params_json, files_json, note FROM change"
            " WHERE node_id = ? ORDER BY technique_id",
            (node_id,),
        ).fetchall()
    ]
    result["evaluations"] = [
        dict(row)
        for row in conn.execute(
            "SELECT evaluation_id, verdict, correctness, reasons_json, protocol_version,"
            " baseline_node_id, created_at FROM evaluation WHERE node_id = ? ORDER BY created_at",
            (node_id,),
        ).fetchall()
    ]
    result["measurements"] = [
        dict(row)
        for row in conn.execute(
            "SELECT evaluation_id, workload_id, role, median_ms, baseline_median_ms, speedup,"
            " noise FROM measurement WHERE node_id = ?",
            (node_id,),
        ).fetchall()
    ]
    result["parents"] = [
        row[0]
        for row in conn.execute(
            "SELECT dst FROM edge WHERE src = ? AND type IN ('DERIVED_FROM','COMBINES')",
            (node_id,),
        ).fetchall()
    ]
    result["children"] = [
        row[0]
        for row in conn.execute(
            "SELECT src FROM edge WHERE dst = ? AND type = 'DERIVED_FROM'", (node_id,)
        ).fetchall()
    ]
    return result
