"""perche: find out which source rows produced each row of a SQL query."""

from __future__ import annotations

import functools

import duckdb

from .rewrite import PREFIX, Rewriter, Source, UnsupportedQuery

__all__ = ["Trace", "UnsupportedQuery", "trace"]
__version__ = "0.1.0"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Trace:
    """The result of a query, with the lineage of every row.

    The traced result stays in a temporary DuckDB table, so lineage is only
    turned into Python objects for the rows you ask about. Source row ids are
    DuckDB rowids; for files they are 0-based data row positions.
    """

    def __init__(self, con, sql: str, table: str, columns: list[str], rows: list[tuple], provs, sources):
        self.con = con
        self.sql = sql  # the rewritten query
        self.columns = columns
        self.rows = rows
        self._table = table
        self._sources: dict[str, Source] = {}
        groups: dict[str, list[str]] = {}
        for p in provs:
            src = sources[p.source]
            self._sources[src.label] = src
            groups.setdefault(src.label, []).append(p.col)
        # One SQL expression per source: the sorted, distinct ids of all its occurrences.
        self._merged = {
            label: "list_sort(list_distinct(flatten(["
            + ", ".join(f"coalesce({_ident(c)}, []::BIGINT[])" for c in cols)
            + "])))"
            for label, cols in groups.items()
        }

    def _select(self, exprs: list[str], where: str = "", params=None) -> list[tuple]:
        if not exprs:
            return [() for _ in self.rows] if not where else [()]
        sql = f"SELECT {', '.join(exprs)} FROM {self._table} {where} ORDER BY rowid"
        return self.con.execute(sql, params or {}).fetchall()

    def lineage_of(self, i: int) -> dict[str, list[int]]:
        """Source row ids behind row ``i``, per source as written in the query."""
        self._check(i)
        (r,) = self._select(list(self._merged.values()), "WHERE rowid = $i", {"i": i})
        return dict(zip(self._merged, r))

    @functools.cached_property
    def lineage(self) -> list[dict[str, list[int]]]:
        """Lineage of every row. Prefer :meth:`lineage_of` on large results."""
        return [dict(zip(self._merged, r)) for r in self._select(list(self._merged.values()))]

    def counts(self, limit: int | None = None) -> list[dict[str, int]]:
        """How many rows of each source produced each of the first ``limit`` rows."""
        where = f"WHERE rowid < {int(limit)}" if limit is not None else ""
        exprs = [f"len({e})" for e in self._merged.values()]
        if not exprs:
            return [{} for _ in self.rows[:limit]]
        return [dict(zip(self._merged, r)) for r in self._select(exprs, where)]

    def why(self, i: int) -> dict[str, list[dict]]:
        """The source rows behind row ``i``, each as a dict with its ``rowid`` first."""
        out = {}
        for label, ids in self.lineage_of(i).items():
            if not ids:
                continue
            cur = self.con.execute(
                f"SELECT rowid, * FROM {self._sources[label].table} "
                "WHERE rowid IN (SELECT unnest($ids::BIGINT[])) ORDER BY rowid",
                {"ids": ids},
            )
            names = [d[0] for d in cur.description]
            out[label] = [dict(zip(names, r)) for r in cur.fetchall()]
        return out

    def close(self) -> None:
        """Drop the temporary table holding the traced result."""
        self.con.execute(f"DROP TABLE IF EXISTS {self._table}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return f"<Trace {len(self.rows)} rows, sources: {', '.join(self._sources) or 'none'}>"

    def _check(self, i: int) -> None:
        if not 0 <= i < len(self.rows):
            raise IndexError(f"row {i} out of range (the query returned {len(self.rows)} rows)")


def trace(sql: str, con: duckdb.DuckDBPyConnection | None = None) -> Trace:
    """Run ``sql`` on ``con`` (default: a new in-memory database) and track row lineage."""
    con = con if con is not None else duckdb.connect()
    rewriter = Rewriter(con)
    tree, provs = rewriter.rewrite(sql)
    traced_sql = tree.sql("duckdb")

    names = con.sql(traced_sql).columns  # binds without running; keeps duplicate names
    table = rewriter.fresh("result_")
    con.execute(f"CREATE TEMP TABLE {table} AS {traced_sql}")
    stored = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    data = [i for i, n in enumerate(names) if not n.startswith(PREFIX)]
    exprs = ", ".join(_ident(stored[i]) for i in data) or "NULL"
    rows = con.execute(f"SELECT {exprs} FROM {table} ORDER BY rowid").fetchall()
    if not data:
        rows = [() for _ in rows]
    return Trace(con, traced_sql, table, [names[i] for i in data], rows, provs, rewriter.sources)
