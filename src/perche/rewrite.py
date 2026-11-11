"""Rewrite a query so that every output row carries the ids of the source rows it came from.

Every row read from a source gets a column holding ``[rowid]``. Operators then
propagate those lists: projections, filters and joins pass them through,
aggregations (GROUP BY, DISTINCT, UNION) concatenate the lists of the rows they
fold together. One list column is kept per source *occurrence*, so a self-join
yields two columns that are merged per source at the end.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass

from typing import Callable, Protocol

import sqlglot
from sqlglot import exp

PREFIX = "__perche_"

# Shared by every rewrite: temp tables live on the connection, so names must never repeat.
_IDS = itertools.count(1)


class UnsupportedQuery(Exception):
    """The query uses a construct whose lineage perche cannot track yet."""


@dataclass(frozen=True)
class Prov:
    col: str  # column holding a LIST of rowids
    source: str  # key into Rewriter.sources


@dataclass(frozen=True)
class Source:
    label: str  # how the user wrote it: table name, file path, function call
    table: str  # table holding the rows, addressable by rowid


def _with(node: exp.Expression) -> exp.With | None:
    return node.args.get("with_") or node.args.get("with")


def _from(node: exp.Expression) -> exp.From | None:
    return node.args.get("from_") or node.args.get("from")


def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="duckdb")


class Catalog(Protocol):
    """What the rewriter needs to know about the database it targets."""

    def has_rowid(self, table_sql: str) -> bool:
        """Whether ``SELECT rowid FROM <table_sql>`` works (a base table)."""

    def copy(self, table_sql: str, name: str) -> None:
        """Copy a view, file or table function into a new table ``name``."""


class DuckDBCatalog:
    def __init__(self, con):
        self.con = con

    def has_rowid(self, table_sql: str) -> bool:
        import duckdb

        try:
            self.con.execute(f"SELECT rowid FROM {table_sql} LIMIT 0")
            return True
        except duckdb.Error:
            return False

    def copy(self, table_sql: str, name: str) -> None:
        self.con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM {table_sql}")


class TableCatalog:
    """Only known base tables; no copies. Used where there is no live connection (the browser demo)."""

    def __init__(self, tables: set[str], copy: Callable[[str, str], None] | None = None):
        self.tables = {t.lower() for t in tables}
        self._copy = copy

    def has_rowid(self, table_sql: str) -> bool:
        return table_sql.strip('"').lower() in self.tables

    def copy(self, table_sql: str, name: str) -> None:
        if self._copy is None:
            raise UnsupportedQuery(f"unknown table {table_sql}; available: {', '.join(sorted(self.tables))}")
        self._copy(table_sql, name)


class Rewriter:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.sources: dict[str, Source] = {}

    def fresh(self, kind: str = "") -> str:
        return f"{PREFIX}{kind}{next(_IDS)}"

    def rewrite(self, sql: str) -> tuple[exp.Expression, list[Prov]]:
        try:
            tree = _parse(sql)
        except sqlglot.errors.ParseError as e:
            raise UnsupportedQuery(f"cannot parse query: {e}") from e
        if not isinstance(tree, exp.Query):
            raise UnsupportedQuery("only SELECT queries can be traced")
        return self.query(tree, {})

    # -- queries -----------------------------------------------------------

    def query(self, node: exp.Expression, ctes: dict) -> tuple[exp.Expression, list[Prov]]:
        if isinstance(node, exp.Subquery):
            inner, provs = self.query(node.this, ctes)
            node.set("this", inner)
            return node, provs
        if isinstance(node, exp.Select):
            return self.select(node, ctes)
        if isinstance(node, exp.SetOperation):
            return self.setop(node, ctes)
        raise UnsupportedQuery(f"cannot trace a {node.key.upper()} query")

    def with_(self, node: exp.Expression, ctes: dict) -> dict:
        """Add a traced copy of every CTE next to the original.

        The originals stay, so subqueries in WHERE or SELECT that read a CTE
        keep seeing its original columns.
        """
        w = _with(node)
        if not w:
            return ctes
        if w.args.get("recursive"):
            raise UnsupportedQuery("recursive CTEs are not supported yet")
        ctes = dict(ctes)
        traced = []
        for cte in w.expressions:
            name = cte.alias_or_name
            body, provs = self.query(cte.this.copy(), ctes)
            new_name = self.fresh(f"cte_{name}_")
            alias = exp.TableAlias(
                this=exp.to_identifier(new_name), columns=cte.args["alias"].args.get("columns")
            )
            traced.append(exp.CTE(this=body, alias=alias))
            ctes[name.lower()] = (new_name, provs)
        w.set("expressions", [*w.expressions, *traced])
        return ctes

    def select(self, node: exp.Select, ctes: dict) -> tuple[exp.Select, list[Prov]]:
        ctes = self.with_(node, ctes)

        scope: list[tuple[str | None, list[Prov]]] = []
        if f := _from(node):
            scope.append(self.source(f.this, ctes))
        for join in node.args.get("joins") or []:
            scope.append(self.source(join.this, ctes))

        self._exclude_from_stars(node, scope)

        distinct = node.args.get("distinct")
        aggregate = self._is_aggregate(node)
        if distinct:
            if distinct.args.get("on"):
                raise UnsupportedQuery("DISTINCT ON is not supported yet")
            if aggregate:
                raise UnsupportedQuery("DISTINCT together with aggregation is not supported yet")
            # SELECT DISTINCT a, b  ==  SELECT a, b ... GROUP BY ALL
            node.set("distinct", None)
            node.set("group", exp.Group(all=True))
            aggregate = True

        out = []
        for qualifier, provs in scope:
            for p in provs:
                ref = exp.column(p.col, table=qualifier)
                value = exp.Flatten(this=exp.ArrayAgg(this=ref)) if aggregate else ref
                name = self.fresh()
                node.select(exp.alias_(value, name), append=True, copy=False)
                out.append(Prov(name, p.source))
        return node, out

    def setop(self, node: exp.SetOperation, ctes: dict) -> tuple[exp.Expression, list[Prov]]:
        """UNION, INTERSECT and EXCEPT.

        Each branch is padded to the same lineage columns (its own filled, the
        other branch's NULL). Everything but UNION ALL then becomes a UNION ALL
        grouped by the data columns, which merges the lineage of equal rows.
        INTERSECT keeps groups seen on both sides, EXCEPT groups seen only on
        the left, so the right side of EXCEPT never shows up in the lineage.
        """
        ctes = self.with_(node, ctes)
        is_union = isinstance(node, exp.Union)
        if not is_union and not node.args.get("distinct"):
            raise UnsupportedQuery(f"{node.key.upper()} ALL is not supported yet")
        left, lp = self.query(node.this, ctes)
        right, rp = self.query(node.expression, ctes)

        out = [Prov(self.fresh(), p.source) for p in lp + rp]
        side = None if is_union else self.fresh("side")
        node.set("this", self._pad(left, lp, out, 0, side and (side, 0)))
        node.set("expression", self._pad(right, rp, out, len(lp), side and (side, 1)))

        if is_union and not node.args.get("distinct"):
            return node, out

        modifiers = {k: node.args.get(k) for k in ("order", "limit", "offset")}
        all_ = exp.Union(this=node.this, expression=node.expression, distinct=False, by_name=node.args.get("by_name"))
        merged = [Prov(self.fresh(), p.source) for p in out]
        aggs = ", ".join(f"FLATTEN(LIST({p.col})) AS {m.col}" for p, m in zip(out, merged))
        having = ""
        if isinstance(node, exp.Intersect):
            having = f" HAVING count(DISTINCT {side}) = 2"
        elif isinstance(node, exp.Except):
            having = f" HAVING max({side}) = 0"
        hidden = ", ".join([p.col for p in out] + ([side] if side else []))
        wrapper = _parse(
            f"SELECT * EXCLUDE ({hidden}), {aggs} "
            f"FROM __placeholder__ AS {self.fresh('u')} GROUP BY ALL{having}"
        )
        _from(wrapper).this.replace(exp.Subquery(this=all_, alias=_from(wrapper).this.args["alias"]))
        for k, v in modifiers.items():
            if v is not None:
                wrapper.set(k, v)
        # A WITH on the set operation must stay visible to both branches.
        if w := _with(node):
            wrapper.set("with_" if "with_" in wrapper.arg_types else "with", w)
        return wrapper, merged

    def _pad(self, branch: exp.Expression, provs: list[Prov], out: list[Prov], offset: int, side=None):
        star = exp.Star()
        if provs:
            star.set("except_", [exp.column(p.col) for p in provs])
        cols: list[exp.Expression] = [star]
        for i, o in enumerate(out):
            if offset <= i < offset + len(provs):
                value = exp.column(provs[i - offset].col)
            else:
                value = exp.cast(exp.null(), "BIGINT[]", dialect="duckdb")
            cols.append(exp.alias_(value, o.col))
        if side:
            name, value = side
            cols.append(exp.alias_(exp.Literal.number(value), name))
        return exp.select(*cols).from_(exp.Subquery(this=branch, alias=exp.TableAlias(this=exp.to_identifier(self.fresh("b")))))

    # -- sources -----------------------------------------------------------

    def source(self, node: exp.Expression, ctes: dict) -> tuple[str | None, list[Prov]]:
        """Rewrite one FROM/JOIN item; return its qualifier and lineage columns."""
        if isinstance(node, exp.Subquery):
            inner, provs = self.query(node.this, ctes)
            node.set("this", inner)
            return node.alias or None, provs

        if not isinstance(node, exp.Table):
            return node.alias or None, []  # VALUES, UNNEST, LATERAL...: no tracked rows

        if isinstance(node.this, exp.Identifier) and not node.args.get("db") and node.name.lower() in ctes:
            traced, provs = ctes[node.name.lower()]
            qualifier = node.alias or node.name
            # Rename the CTE's lineage columns so two references never share names.
            renamed = [Prov(self.fresh(), p.source) for p in provs]
            # Lineage columns always come last, so EXCLUDE + re-append keeps column order.
            star = exp.Star()
            if provs:
                star.set("except_", [exp.column(p.col) for p in provs])
            sub = exp.select(star, *(exp.alias_(exp.column(p.col), r.col) for p, r in zip(provs, renamed)))
            sub = sub.from_(traced)
            node.replace(exp.Subquery(this=sub, alias=self._alias(node, qualifier)))
            return qualifier, renamed

        return self.base(node)

    def base(self, node: exp.Table) -> tuple[str, list[Prov]]:
        """A table, view, file or table function: give each of its rows an id."""
        bare = node.copy()
        bare.set("alias", None)
        key = bare.sql("duckdb")
        if key not in self.sources:
            if self.catalog.has_rowid(key):
                table = key
            else:
                # Views, files and table functions have no stable rowid: copy them once.
                table = self.fresh("src_")
                self.catalog.copy(key, table)
            self.sources[key] = Source(self._label(bare), table)

        qualifier = node.alias or self._default_alias(bare)
        col = self.fresh()
        sub = _parse(f"SELECT *, [rowid] AS {col} FROM {self.sources[key].table}")
        node.replace(exp.Subquery(this=sub, alias=self._alias(node, qualifier)))
        return qualifier, [Prov(col, key)]

    @staticmethod
    def _label(bare: exp.Table) -> str:
        if isinstance(bare.this, exp.Identifier) and bare.this.quoted and not bare.args.get("db"):
            return bare.name  # 'orders.csv'
        return bare.sql("duckdb")

    @staticmethod
    def _default_alias(bare: exp.Table) -> str:
        """The name DuckDB would give the source when the query does not alias it."""
        if isinstance(bare.this, exp.Identifier):
            name = bare.name
            if bare.this.quoted and ("." in name or "/" in name):
                return os.path.basename(name).split(".")[0]
            return name
        if isinstance(bare.this, exp.Func):
            return bare.this.sql_name().lower() if not isinstance(bare.this, exp.Anonymous) else bare.this.name
        return bare.sql("duckdb")

    @staticmethod
    def _alias(node: exp.Expression, qualifier: str) -> exp.TableAlias:
        old = node.args.get("alias")
        columns = old.args.get("columns") if old else None
        return exp.TableAlias(this=exp.to_identifier(qualifier), columns=columns)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _exclude_from_stars(node: exp.Select, scope) -> None:
        """Keep lineage columns out of the user's SELECT * and t.*."""
        every = sorted({p.col for _, provs in scope for p in provs})
        by_qualifier = {(q or "").lower(): [p.col for p in provs] for q, provs in scope}
        for e in node.expressions:
            if isinstance(e, exp.Star):
                star, names = e, every
            elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
                star, names = e.this, by_qualifier.get(e.table.lower(), [])
            else:
                continue
            if names:
                star.set("except_", [*(star.args.get("except_") or []), *map(exp.column, names)])

    @staticmethod
    def _is_aggregate(node: exp.Select) -> bool:
        if node.args.get("group") or node.args.get("having"):
            return True
        for e in node.expressions:
            for agg in e.find_all(exp.AggFunc):
                if agg.find_ancestor(exp.Window, exp.Select) is node:
                    return True
        return False


def rewrite(sql: str, tables: set[str]) -> dict:
    """Rewrite ``sql`` for a database holding ``tables``, without connecting to it.

    Returns the traced SQL and its lineage columns, each with the source label
    and the table to read rows from. The browser demo calls this from Pyodide.
    """
    rw = Rewriter(TableCatalog(tables))
    tree, provs = rw.rewrite(sql)
    return {
        "sql": tree.sql("duckdb"),
        "lineage": [
            {"column": p.col, "source": rw.sources[p.source].label, "table": rw.sources[p.source].table}
            for p in provs
        ],
    }
