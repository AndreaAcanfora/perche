"""Random queries checked against independent ground truth.

1. Exact oracle: for single-block queries the generator also writes an oracle
   query that collects the real rowids by hand (``[c.rowid]`` per row,
   ``list(DISTINCT c.rowid)`` per group). perche must return the same lineage.
2. Metamorphic: wrapping a query in a subquery, a CTE or ``UNION ALL`` with
   itself must not change any row's lineage.
3. Sufficiency: re-running the query on *only* the lineage rows of a result
   row must reproduce that row.
"""

import functools
import random
from collections import Counter

import pytest

import perche
from conftest import make_shop

shop = functools.lru_cache(maxsize=None)(make_shop)

TABLES = {"c": "customers", "o": "orders", "i": "items"}
JOINS = [
    ["c"],
    ["o"],
    ["c", "o"],
    ["c", "o", "i"],
    ["c", "o:left"],
    ["o", "i:left"],
]
ON = {"o": "o.customer_id = c.id", "i": "i.order_id = o.id"}
PREDICATES = {
    "c": ["c.country = 'IT'", "c.tier >= 2", "c.country IS NULL", "c.name LIKE 'c1%'"],
    "o": ["o.amount > 50", "o.status = 'paid'", "o.amount IS NULL", "o.customer_id IN (1, 2, 3, 4)"],
    "i": ["i.qty > 2", "i.sku IN ('A', 'B')"],
}
COLUMNS = {
    "c": ["c.country", "c.tier", "c.name"],
    "o": ["o.status", "o.customer_id"],
    "i": ["i.sku"],
}
AGGS = {"c": ["count(c.id)", "max(c.tier)"], "o": ["sum(o.amount)", "count(*)", "avg(o.amount)"], "i": ["sum(i.qty)"]}


def generate(rnd: random.Random) -> tuple[str, str, list[str]]:
    """Return (query, oracle, aliases)."""
    shape = rnd.choice(JOINS)
    aliases = [a.split(":")[0] for a in shape]
    sql_from = f"{TABLES[aliases[0]]} {aliases[0]}"
    for a in shape[1:]:
        alias, _, kind = a.partition(":")
        sql_from += f" {'LEFT JOIN' if kind else 'JOIN'} {TABLES[alias]} {alias} ON {ON[alias]}"

    preds = [rnd.choice(PREDICATES[a]) for a in aliases if rnd.random() < 0.5]
    glue = f" {rnd.choice(['AND', 'OR'])} "
    where = f" WHERE {glue.join(preds)}" if preds else ""
    cols = rnd.sample([c for a in aliases for c in COLUMNS[a]], k=rnd.randint(1, 2))
    mode = rnd.choice(["plain", "group", "distinct", "global"])

    if mode == "plain":
        query = f"SELECT {', '.join(cols)} FROM {sql_from}{where}"
        ids = [f"CASE WHEN {a}.rowid IS NULL THEN [] ELSE [{a}.rowid] END" for a in aliases]
        oracle = f"SELECT {', '.join(cols)}, {', '.join(ids)} FROM {sql_from}{where}"
        return query, oracle, aliases

    ids = [f"coalesce(list(DISTINCT {a}.rowid) FILTER (WHERE {a}.rowid IS NOT NULL), [])" for a in aliases]
    if mode == "distinct":
        query = f"SELECT DISTINCT {', '.join(cols)} FROM {sql_from}{where}"
        oracle = f"SELECT {', '.join(cols)}, {', '.join(ids)} FROM {sql_from}{where} GROUP BY ALL"
        return query, oracle, aliases

    aggs = [rnd.choice(AGGS[a]) for a in rnd.sample(aliases, k=1)]
    keys = [] if mode == "global" else cols
    group = f" GROUP BY {', '.join(keys)}" if keys else ""
    query = f"SELECT {', '.join(keys + aggs)} FROM {sql_from}{where}{group}"
    oracle = f"SELECT {', '.join(keys + aggs)}, {', '.join(ids)} FROM {sql_from}{where}{group}"
    return query, oracle, aliases


def lineage_bag(t: perche.Trace, tables: list[str]) -> Counter:
    return Counter(
        (repr(row), tuple(tuple(lin.get(tb, [])) for tb in tables)) for row, lin in zip(t.rows, t.lineage)
    )


def oracle_bag(con, oracle: str, aliases: list[str]) -> Counter:
    """Merge per-alias rowids into per-table lineage, like perche does."""
    tables = sorted({TABLES[a] for a in aliases})
    bag = Counter()
    for r in con.execute(oracle).fetchall():
        data, ids = r[: len(r) - len(aliases)], r[len(r) - len(aliases) :]
        merged = {tb: set() for tb in tables}
        for a, xs in zip(aliases, ids):
            merged[TABLES[a]].update(xs)
        bag[(repr(tuple(data)), tuple(tuple(sorted(merged[tb])) for tb in tables))] += 1
    return bag


SEEDS = range(300)


@pytest.mark.parametrize("seed", SEEDS)
def test_matches_oracle(seed):
    rnd = random.Random(seed)
    con = shop(seed % 7)
    query, oracle, aliases = generate(rnd)
    tables = sorted({TABLES[a] for a in aliases})
    t = perche.trace(query, con)
    assert lineage_bag(t, tables) == oracle_bag(con, oracle, aliases), query


@pytest.mark.parametrize("seed", SEEDS)
def test_wrapping_preserves_lineage(seed):
    rnd = random.Random(seed)
    con = shop(seed % 7)
    query, _, aliases = generate(rnd)
    tables = sorted({TABLES[a] for a in aliases})
    base = lineage_bag(perche.trace(query, con), tables)

    assert lineage_bag(perche.trace(f"SELECT * FROM ({query}) AS q", con), tables) == base
    assert lineage_bag(perche.trace(f"WITH q AS ({query}) SELECT q.* FROM q", con), tables) == base
    doubled = perche.trace(f"SELECT * FROM ({query}) a UNION ALL SELECT * FROM ({query}) b", con)
    assert lineage_bag(doubled, tables) == Counter({k: 2 * v for k, v in base.items()})

    # Set operations of a query with itself: same rows and lineage as DISTINCT.
    distinct = lineage_bag(perche.trace(f"SELECT DISTINCT * FROM ({query}) d", con), tables)
    assert lineage_bag(perche.trace(f"({query}) INTERSECT ({query})", con), tables) == distinct
    assert lineage_bag(perche.trace(f"({query}) UNION ({query})", con), tables) == distinct
    nothing = f"SELECT * FROM ({query}) e WHERE false"
    assert lineage_bag(perche.trace(f"({query}) EXCEPT ({nothing})", con), tables) == distinct


@pytest.mark.parametrize("seed", range(80))
def test_lineage_alone_reproduces_the_row(seed):
    rnd = random.Random(seed)
    con = shop(seed % 7)
    query, _, _ = generate(rnd)
    # Compose a little: a CTE on top of the generated block, and sometimes a UNION.
    if rnd.random() < 0.5:
        query = f"WITH q AS ({query}) SELECT * FROM q"
    if rnd.random() < 0.3:
        op = rnd.choice(["UNION", "INTERSECT", "EXCEPT"])
        first = perche.trace(query, con).columns[0]
        half = f'hash(z."{first}") % 2 = 0'
        query = f"({query}) {op} (SELECT * FROM ({query}) z WHERE {half})"
    t = perche.trace(query, con)

    for row, lin in list(zip(t.rows, t.lineage))[:15]:
        con.execute("ATTACH ':memory:' AS lineage_only")
        for table in TABLES.values():
            ids = lin.get(table, [])
            con.execute(
                f"CREATE TABLE lineage_only.{table} AS SELECT * FROM memory.{table} "
                "WHERE rowid IN (SELECT unnest($ids::BIGINT[])) ORDER BY rowid",
                {"ids": ids},
            )
        con.execute("USE lineage_only")
        again = con.execute(query).fetchall()
        con.execute("USE memory")
        con.execute("DETACH lineage_only")
        assert row in again, (query, row, lin)
