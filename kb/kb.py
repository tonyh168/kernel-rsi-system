#!/usr/bin/env python3
"""kernel-kb command line interface.

Run either of these; both work from any directory:

    python3 kb/kb.py rebuild --runs runs --profiles configs
    python3 -m kb.kb rebuild --runs runs --profiles configs

The database defaults to `<this directory>/kb.db` so the whole `kb/` folder can
be copied elsewhere and keep working.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import digest as digest_mod  # noqa: E402
import project  # noqa: E402

DEFAULT_DB = HERE / "kb.db"


def connect(path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(
                f"database not found: {path}\n"
                f"build it first:  python3 {HERE.name}/kb.py rebuild --runs <runs-dir>"
            )
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def render_table(columns: list[str], rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "(0 rows)"
    values = [[("" if row[c] is None else str(row[c])) for c in columns] for row in rows]
    widths = [max(len(c), *(len(v[i]) for v in values)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))]
    lines.append("  ".join("-" * w for w in widths))
    for value in values:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(value)))
    return "\n".join(lines)


# ---------------------------------------------------------------------- commands


def cmd_rebuild(args: argparse.Namespace) -> int:
    stats = project.rebuild(
        Path(args.db),
        Path(args.runs),
        [Path(p) for p in (args.profiles or [])],
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_q(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    try:
        cursor = conn.execute(args.sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchall()
        if args.json:
            print(json.dumps([{c: row[c] for c in columns} for row in rows], ensure_ascii=False, indent=2))
        else:
            print(render_table(columns, rows))
        return 0
    finally:
        conn.close()


def cmd_card(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    try:
        data = digest_mod.card(conn, args.node)
        if data is None:
            print(f"node not found: {args.node}", file=sys.stderr)
            return 1
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"# {data['node_id']}  [{data['status']}]")
            print(f"- operator: {data['operator']}")
            print(f"- lane: {data['lane_id']}")
            print(f"- uid: {data['uid']}")
            print(f"- generation: {data['generation']}  parents: {', '.join(data['parents']) or '-'}")
            print(f"- hypothesis: {data['hypothesis'] or '-'}")
            if data["failure_reason"]:
                print(f"- failure: {data['failure_kind']} — {data['failure_reason']}")
            if data["changes"]:
                print("\n## changes")
                for change in data["changes"]:
                    print(f"- `{change['technique_id']}` ({change['role']}) {change['note']}")
            if data["measurements"]:
                print("\n## measurements")
                for m in data["measurements"]:
                    print(
                        f"- {m['workload_id']} [{m['role']}] "
                        f"speedup={m['speedup']} noise={m['noise']}"
                    )
            if data["evaluations"]:
                print("\n## evaluations")
                for e in data["evaluations"]:
                    print(f"- `{e['evaluation_id']}` {e['verdict']} ({e['correctness']})")
        return 0
    finally:
        conn.close()


def cmd_digest(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    try:
        if args.json:
            lanes = conn.execute(
                "SELECT * FROM lane WHERE operator = ?", (args.operator,)
            ).fetchall()
            payload = {
                "operator": args.operator,
                "lanes": [dict(row) for row in lanes],
                "markdown": digest_mod.render(conn, args.operator, vendor=args.vendor),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(digest_mod.render(conn, args.operator, vendor=args.vendor))
        return 0
    finally:
        conn.close()


def cmd_lanes(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    try:
        rows = conn.execute(
            "SELECT l.lane_id, l.operator, l.vendor, l.device, l.dtype, l.protocol_version,"
            " COUNT(n.node_id) AS nodes,"
            " (SELECT node_id FROM v_champion c WHERE c.lane_id = l.lane_id"
            "  ORDER BY c.generation DESC LIMIT 1) AS champion"
            " FROM lane l LEFT JOIN node n ON n.lane_id = l.lane_id"
            " GROUP BY l.lane_id ORDER BY nodes DESC"
        ).fetchall()
        print(render_table(["lane_id", "operator", "vendor", "device", "dtype",
                            "protocol_version", "nodes", "champion"], rows))
        return 0
    finally:
        conn.close()


def cmd_tech(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db), readonly=args.tech_command == "list")
    try:
        if args.tech_command == "list":
            rows = conn.execute(
                "SELECT technique_id, family, status, origin, description FROM technique"
                " ORDER BY status, family, technique_id"
            ).fetchall()
            print(render_table(["technique_id", "family", "status", "origin", "description"], rows))
        elif args.tech_command == "add":
            conn.execute(
                "INSERT OR REPLACE INTO technique(technique_id, family, kind, description,"
                " aliases_json, status, merged_into, applies_when_json, origin)"
                " VALUES (?,?,?,?,'[]','stable',NULL,'{}','manual')",
                (args.id, args.family, args.family, args.description or ""),
            )
            conn.commit()
            print(f"added {args.id}")
        elif args.tech_command == "merge":
            conn.execute(
                "UPDATE technique SET status = 'deprecated', merged_into = ? WHERE technique_id = ?",
                (args.into, args.id),
            )
            conn.execute(
                "UPDATE change SET technique_id = ? WHERE technique_id = ?", (args.into, args.id)
            )
            conn.commit()
            print(f"merged {args.id} -> {args.into}")
        return 0
    finally:
        conn.close()


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    try:
        for table in ("lane", "node", "edge", "change", "evaluation", "measurement", "technique"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:12s} {count}")
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta")}
        print(f"\nschema_version {meta.get('schema_version', '?')}")
        return 0
    finally:
        conn.close()


# ------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb", description="Knowledge base over kernel experiments")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path (default: kb/kb.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    rebuild = sub.add_parser("rebuild", help="Re-ingest every run into the database")
    rebuild.add_argument("--runs", required=True, help="run dir, or a dir containing run dirs")
    rebuild.add_argument("--profiles", action="append", default=[],
                         help="DeviceProfile json file or directory (repeatable)")
    rebuild.set_defaults(func=cmd_rebuild)

    q = sub.add_parser("q", help="Run a read-only SQL query")
    q.add_argument("sql")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_q)

    card = sub.add_parser("card", help="Show one node and its evidence")
    card.add_argument("node")
    card.add_argument("--format", choices=["md", "json"], default="md")
    card.set_defaults(func=cmd_card)

    dig = sub.add_parser("digest", help="Agent-facing summary for one operator")
    dig.add_argument("--operator", required=True)
    dig.add_argument("--vendor")
    dig.add_argument("--json", action="store_true")
    dig.set_defaults(func=cmd_digest)

    lanes = sub.add_parser("lanes", help="List comparability domains")
    lanes.set_defaults(func=cmd_lanes)

    tech = sub.add_parser("tech", help="Inspect or curate the technique vocabulary")
    tech_sub = tech.add_subparsers(dest="tech_command", required=True)
    tech_sub.add_parser("list").set_defaults(func=cmd_tech)
    add = tech_sub.add_parser("add")
    add.add_argument("id")
    add.add_argument("--family", default="other")
    add.add_argument("--description", default="")
    add.set_defaults(func=cmd_tech)
    merge = tech_sub.add_parser("merge")
    merge.add_argument("id")
    merge.add_argument("--into", required=True)
    merge.set_defaults(func=cmd_tech)

    stats = sub.add_parser("stats", help="Row counts")
    stats.set_defaults(func=cmd_stats)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(args.func(args))
    except (FileNotFoundError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
