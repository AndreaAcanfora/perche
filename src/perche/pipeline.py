"""Pipelines: SQL steps that read files and earlier steps, with lineage across all of them.

Each step's result becomes a table named after the step, whose rowids are the
step's row numbers. A later step reading that table gets lineage in terms of
those rowids, and :meth:`Pipeline.why` follows them back step by step to the
original sources.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from . import PREFIX, Trace, UnsupportedQuery, _ident, trace


class Pipeline:
    def __init__(self, steps: dict[str, str], con: duckdb.DuckDBPyConnection | None = None):
        self.con = con if con is not None else duckdb.connect()
        self.steps = {name.lower(): sql for name, sql in steps.items()}
        self.order = _order(self.steps)
        self.traces: dict[str, Trace] = {}

    @classmethod
    def from_dir(cls, path: str | Path, con: duckdb.DuckDBPyConnection | None = None) -> Pipeline:
        """One step per ``*.sql`` file, named after the file. Relative file paths
        in the queries are resolved from the directory."""
        path = Path(path)
        files = sorted(path.glob("*.sql"))
        if not files:
            raise ValueError(f"no .sql files in {path}")
        con = con if con is not None else duckdb.connect()
        con.execute("SET file_search_path = $p", {"p": str(path.resolve())})
        return cls({f.stem: f.read_text() for f in files}, con)

    def run(self) -> Pipeline:
        for name in self.order:
            t = trace(self.steps[name], self.con)
            # Insertion order is kept, so the table's rowids are the trace's row numbers.
            self.con.execute(
                f"CREATE OR REPLACE TABLE {_ident(name)} AS "
                f"SELECT COLUMNS(c -> NOT starts_with(c, '{PREFIX}')) FROM {t._table} ORDER BY rowid"
            )
            self.traces[name] = t
        return self

    def __getitem__(self, step: str) -> Trace:
        return self.traces[step.lower()]

    @property
    def last(self) -> str:
        return self.order[-1]

    def _step_of(self, label: str) -> str | None:
        name = label.lower()
        return name if name in self.traces else None

    def roots(self, step: str, ids: list[int]) -> dict[str, list[int]]:
        """Original source rows (not earlier steps) behind rows ``ids`` of ``step``."""
        out: dict[str, set[int]] = {}
        for label, sub in self[step].lineage_of_rows(ids).items():
            inner = self._step_of(label)
            found = self.roots(inner, sub) if inner and sub else {} if inner else {label: sub}
            for k, v in found.items():
                out.setdefault(k, set()).update(v)
        return {k: sorted(v) for k, v in out.items()}

    def why(self, step: str, i: int) -> dict:
        """The tree of rows behind row ``i`` of ``step``, down to the original sources."""
        t = self[step]
        t._check(i)
        return {"step": step.lower(), "rows": [dict(zip(t.columns, t.rows[i]))], "from": self._children(step, [i])}

    def _children(self, step: str, ids: list[int]) -> list[dict]:
        t = self[step]
        out = []
        for label, sub in t.lineage_of_rows(ids).items():
            if not sub:
                continue
            inner = self._step_of(label)
            if inner:
                cur = self.con.execute(
                    f"SELECT rowid, * FROM {_ident(inner)} WHERE rowid IN (SELECT unnest($ids::BIGINT[])) ORDER BY rowid",
                    {"ids": sub},
                )
                names = [d[0] for d in cur.description]
                rows = [dict(zip(names, r)) for r in cur.fetchall()]
                out.append({"step": inner, "rows": rows, "from": self._children(inner, sub)})
            else:
                out.append({"source": label, "rows": t.source_rows(label, sub)})
        return out


def _order(steps: dict[str, str]) -> list[str]:
    """Steps in dependency order; ties keep the given order."""
    deps = {}
    for name, sql in steps.items():
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except sqlglot.errors.ParseError as e:
            raise UnsupportedQuery(f"step {name}: cannot parse query: {e}") from e
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        read = {t.name.lower() for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Identifier)}
        deps[name] = {d for d in read - ctes if d in steps and d != name}

    order: list[str] = []
    while len(order) < len(steps):
        ready = [n for n in steps if n not in order and deps[n] <= set(order)]
        if not ready:
            cycle = sorted(n for n in steps if n not in order)
            raise ValueError(f"steps depend on each other in a cycle: {', '.join(cycle)}")
        order.append(ready[0])
    return order
