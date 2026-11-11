# perche

**`git blame` for data.** Run a SQL query, point at any row of the result, and see the exact source rows that produced it, through every join, filter, `GROUP BY`, `DISTINCT`, CTE, `UNION`, `INTERSECT` and `EXCEPT`.

*Perché* is Italian for “why”.

```console
$ perche "SELECT c.country, sum(o.amount) AS revenue
          FROM 'orders.csv' o JOIN 'customers.csv' c ON c.id = o.customer_id
          WHERE o.status = 'paid' GROUP BY c.country ORDER BY revenue DESC"

#  country  revenue  ← source rows
─  ───────  ───────  ─────────────────────────────
0  IT       2212.78  orders.csv:5  customers.csv:3
1  FR       42       orders.csv:1  customers.csv:1
```

Italy looks suspiciously high. Why?

```console
$ perche "…same query…" --row 0

row 0:
  country  revenue
  ───────  ───────
  IT       2212.78

← orders.csv  (5 rows)
  rowid  id   customer_id  amount  status
  ─────  ───  ───────────  ──────  ──────
  0      101  1            120     paid
  1      102  2            80.5    paid
  4      105  4            999.99  paid
  5      106  4            999.99  paid     ← the same order, loaded twice
  6      107  2            12.3    paid

← customers.csv  (3 rows)
  rowid  id  name   country
  ─────  ──  ─────  ───────
  0      1   Ada    IT
  1      2   Bruno  IT
  3      4   Dario  IT
```

No server and no setup: perche runs on [DuckDB](https://duckdb.org), so the sources can be CSV, Parquet or JSON files, DuckDB tables, or anything DuckDB can read.

## Install

```console
pip install perche
```

Python 3.10+. The only dependencies are `duckdb` and `sqlglot`.

## Command line

```console
perche QUERY                  # the result, with how many source rows feed each row
perche QUERY --row N          # the source rows behind row N
perche QUERY --row N --json   # the same, as JSON
perche QUERY --db shop.duckdb # query tables in a DuckDB database (opened read-only)
perche QUERY --sql            # print the rewritten query perche runs
perche - < query.sql          # read the query from stdin
```

## Python

```python
import duckdb
import perche

con = duckdb.connect("shop.duckdb")
t = perche.trace("SELECT country, count(*) FROM customers GROUP BY 1", con)

t.rows            # [('IT', 3), ('FR', 1)]
t.lineage_of(0)   # {'customers': [0, 1, 3]}   rowids, per source
t.why(0)          # {'customers': [{'rowid': 0, 'id': 1, 'name': 'Ada', ...}, ...]}
t.counts()        # [{'customers': 3}, {'customers': 1}]
t.lineage         # lineage of every row (costly on big results)
```

## How it works

perche parses the query with [sqlglot](https://github.com/tobymao/sqlglot) and rewrites it so that every row carries the ids of the source rows it came from:

- each table, file or view read by the query gets an extra column holding `[rowid]`;
- projections, filters, joins, `ORDER BY` and `LIMIT` pass those lists through;
- `GROUP BY`, aggregates, `DISTINCT`, `UNION` and `INTERSECT` concatenate the lists of the rows they fold together; `EXCEPT` keeps only the left side's lists, since the right side's rows are the ones that did not match;
- CTEs and subqueries are rewritten recursively; each reference to a CTE gets its own lineage columns, so self-joins work.

The rewritten query (see it with `--sql`) runs in DuckDB, and the result stays in a temporary table. Python only sees the ids of the rows you ask about.

Files, views and table functions have no stable rowid, so perche copies them once into a temporary table. For a file, the row id is the 0-based position of the data row.

This is *lineage* in the sense of [Cui and Widom](https://dl.acm.org/doi/10.1145/357775.357777): for an aggregate row, every input row of its group; for a join row, one row per joined source.

## Is it right?

The tests check perche against ground truth computed independently:

- **Exact oracle.** 300 random queries over a shop database with NULLs, orphans and duplicates (inner and left joins across up to three tables, AND/OR filters, `GROUP BY`, global aggregates, `DISTINCT`). For each query the generator also writes an oracle query that collects the rowids by hand. Both must produce exactly the same lineage.
- **Metamorphic.** Wrapping each query in a subquery, in a CTE, or in a `UNION ALL` with itself must not change any row's lineage; `INTERSECT`, `UNION` and `EXCEPT` of a query with itself must match its `DISTINCT`.
- **Sufficiency.** Re-running a query, composed with CTEs, `UNION`, `INTERSECT` and `EXCEPT`, on *only* the lineage rows of a result row must produce that row again.

Breaking the engine on purpose shows the tests notice: dropping the lineage of joined tables fails 233 of them, and treating `DISTINCT` like a projection fails 63.

## Cost

Measured on 1M orders joined with 50k customers on a 4-core machine, DuckDB 1.5 (`python bench/overhead.py`):

| query | plain | traced | |
|---|---|---|---|
| filter | 0.009 s | 0.017 s | 1.9× |
| join + group by (4 groups) | 0.020 s | 0.127 s | 6.2× |
| group by (50k groups) | 0.057 s | 0.230 s | 4.0× |

Lineage for an aggregate holds every input row id of its group, so a query that folds a billion rows into one number carries a billion ids.

## Not tracked yet

perche raises `UnsupportedQuery` instead of guessing for `INTERSECT ALL`, `EXCEPT ALL`, recursive CTEs, `DISTINCT ON` and `DISTINCT` combined with aggregation.

These are accepted, but not everything they read shows up in the lineage:

- **Subqueries** in `WHERE`, `SELECT` or `HAVING` (`WHERE x IN (SELECT …)`, scalar subqueries): their rows are not part of the lineage.
- **Window functions:** a row's lineage is its own input row, not the whole window.
- **`VALUES`, `UNNEST` and `LATERAL`** in `FROM` contribute no rows.
- **Whole-row references** such as `hash(t)` or `SELECT t FROM t` see perche's hidden lineage columns as extra fields, so their values change. Name the columns instead.

## Prior art

Row-level provenance has been studied for years in database research. perche is a small, practical take on it, not a new idea:

- [ProvSQL](https://github.com/PierreSenellart/provsql): PostgreSQL extension for provenance and probabilistic databases, with semiring provenance.
- [Perm](https://www.cs.uic.edu/~bglavic/dbgroup/assets/pdfpubls/GM13.pdf) and [GProM](https://github.com/IITDBGroup/gprom): provenance through query rewriting, the same approach perche uses.
- [SmokedDuck](https://haneensa.github.io/smokedduck-intro/): a DuckDB fork that captures lineage inside the engine, much faster than rewriting.

perche needs no database server, extension or fork. It works on files, and its goal is to answer one question quickly: where did this number come from?

## Roadmap

- Pipelines: lineage across several steps (`raw → clean → report`), each a SQL file.
- A browser dashboard where clicking a chart shows the source rows behind it, running entirely on DuckDB-Wasm.
- Lineage through subqueries in `WHERE` (`IN`, `EXISTS`).

## License

MIT
