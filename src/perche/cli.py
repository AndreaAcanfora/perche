"""perche command line: run a query and ask where a row comes from."""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import sys

import duckdb

from . import UnsupportedQuery, __version__, trace


def _cell(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return format(v, ".15g")
    return str(v)


def _table(header: list[str], rows: list[list[str]], indent: str = "") -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(header)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip()]
    lines.append("  ".join("─" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(indent + line for line in lines)


def _json_default(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    return str(v)


def _print_tree(node: dict, depth: int = 0) -> None:
    pad = "    " * depth
    for child in node["from"]:
        rows = child["rows"]
        noun = "row" if len(rows) == 1 else "rows"
        kind = "step " if "step" in child else ""
        print(f"\n{pad}← {kind}{child.get('step') or child.get('source')}  ({len(rows)} {noun})")
        header = list(rows[0])
        print(_table(header, [[_cell(r[h]) for h in header] for r in rows], pad + "  "))
        if "from" in child:
            _print_tree(child, depth + 1)


def run_main(argv: list[str]) -> int:
    from .pipeline import Pipeline

    p = argparse.ArgumentParser(
        prog="perche run",
        description="Run a pipeline (a directory of .sql files, one step each) and trace rows across steps.",
    )
    p.add_argument("dir", help="directory with one .sql file per step; steps read files and each other by name")
    p.add_argument("-r", "--row", help="show the rows behind row N of the last step, or STEP:N")
    p.add_argument("--db", help="DuckDB database file to run in (step tables are written there)")
    p.add_argument("--limit", type=int, default=50, help="result rows to print (default 50)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = p.parse_args(argv)

    try:
        pipe = Pipeline.from_dir(args.dir, duckdb.connect(args.db) if args.db else None).run()
    except (UnsupportedQuery, ValueError, duckdb.Error) as e:
        print(f"perche: {e}", file=sys.stderr)
        return 2

    if args.row is not None:
        step, _, n = args.row.rpartition(":")
        step = step or pipe.last
        try:
            tree = pipe.why(step, int(n))
        except (KeyError, ValueError, IndexError) as e:
            print(f"perche: bad --row {args.row}: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(tree, default=_json_default, indent=2, ensure_ascii=False))
            return 0
        t = pipe[step]
        print(f"{tree['step']} row {n}:")
        print(_table(t.columns, [[_cell(v) for v in t.rows[int(n)]]], "  "))
        _print_tree(tree)
        return 0

    t = pipe[pipe.last]
    shown = t.rows[: args.limit]
    roots = [pipe.roots(pipe.last, [i]) for i in range(len(shown))]
    if args.json:
        out = [{"row": dict(zip(t.columns, r)), "lineage": lin} for r, lin in zip(shown, roots)]
        print(json.dumps(out, default=_json_default, indent=2, ensure_ascii=False))
        return 0
    for name in pipe.order:
        sources = ", ".join(pipe[name]._merged) or "nothing"
        print(f"{name:20} {len(pipe[name]):>8} rows  ← {sources}")
    print(f"\n{pipe.last}:")
    body = [
        [str(i), *(_cell(v) for v in r), "  ".join(f"{k}:{len(v)}" for k, v in lin.items())]
        for i, (r, lin) in enumerate(zip(shown, roots))
    ]
    print(_table(["#", *t.columns, "← source rows"], body))
    if len(t.rows) > len(shown):
        print(f"… {len(t.rows) - len(shown)} more rows")
    if t.rows:
        print(f"\nperche run {args.dir} --row N  shows where row N comes from, through every step")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["run"]:
        return run_main(argv[1:])

    p = argparse.ArgumentParser(
        prog="perche",
        description="Run a SQL query and find out which source rows produced each result row.",
        epilog="Sources can be DuckDB tables (--db) or files referenced in the query: FROM 'orders.csv'. "
        "To trace a pipeline of several .sql files: perche run DIR.",
    )
    p.add_argument("query", help="SQL query, or - to read it from stdin")
    p.add_argument("-r", "--row", type=int, help="show the source rows behind result row N")
    p.add_argument("--db", help="DuckDB database file to query (opened read-only)")
    p.add_argument("--limit", type=int, default=50, help="result rows to print (default 50)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p.add_argument("--sql", action="store_true", help="print the rewritten query and exit")
    p.add_argument("--version", action="version", version=f"perche {__version__}")
    args = p.parse_args(argv)

    sql = sys.stdin.read() if args.query == "-" else args.query
    con = duckdb.connect(args.db, read_only=True) if args.db else duckdb.connect()
    try:
        t = trace(sql, con)
    except (UnsupportedQuery, duckdb.Error) as e:
        print(f"perche: {e}", file=sys.stderr)
        return 2

    if args.sql:
        print(t.sql)
        return 0

    if args.row is not None:
        if not 0 <= args.row < len(t.rows):
            print(f"perche: row {args.row} out of range (the query returned {len(t.rows)} rows)", file=sys.stderr)
            return 2
        sources = t.why(args.row)
        if args.json:
            row = dict(zip(t.columns, t.rows[args.row]))
            print(json.dumps({"row": row, "sources": sources}, default=_json_default, indent=2, ensure_ascii=False))
            return 0
        print(f"row {args.row}:")
        print(_table(t.columns, [[_cell(v) for v in t.rows[args.row]]], "  "))
        if not sources:
            print("\nno source rows (a constant, or an aggregate over nothing)")
        for label, rows in sources.items():
            noun = "row" if len(rows) == 1 else "rows"
            print(f"\n← {label}  ({len(rows)} {noun})")
            header = list(rows[0])
            print(_table(header, [[_cell(r[h]) for h in header] for r in rows], "  "))
        return 0

    if args.json:
        out = [
            {"row": dict(zip(t.columns, r)), "lineage": lin}
            for r, lin in zip(t.rows, t.lineage)
        ]
        print(json.dumps(out, default=_json_default, indent=2, ensure_ascii=False))
        return 0

    shown = t.rows[: args.limit]
    body = [
        [str(i), *(_cell(v) for v in r), "  ".join(f"{k}:{n}" for k, n in counts.items())]
        for i, (r, counts) in enumerate(zip(shown, t.counts(args.limit)))
    ]
    print(_table(["#", *t.columns, "← source rows"], body))
    if len(t.rows) > len(shown):
        print(f"… {len(t.rows) - len(shown)} more rows")
    if t.rows:
        print("\nperche QUERY --row N  shows where row N comes from")
    return 0


if __name__ == "__main__":
    sys.exit(main())
