"""Overhead of tracing vs running the plain query, on generated data."""

import sys
import time

import duckdb

import perche

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
con = duckdb.connect()
con.execute(f"CREATE TABLE orders AS SELECT range AS id, range % 50000 AS customer_id, (range * 7919) % 1000 / 10.0 AS amount FROM range({N})")
con.execute("CREATE TABLE customers AS SELECT range AS id, ['IT','FR','DE','ES'][range % 4 + 1] AS country FROM range(50000)")

QUERIES = {
    "filter": "SELECT * FROM orders WHERE amount > 99.5",
    "join + group by (4 groups)": "SELECT c.country, sum(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY 1",
    "group by (50k groups)": "SELECT customer_id, count(*) FROM orders GROUP BY 1",
}


def best(f, n=3):
    times = []
    for _ in range(n):
        t = time.perf_counter()
        f()
        times.append(time.perf_counter() - t)
    return min(times)


print(f"{N:,} orders, 50,000 customers, DuckDB {duckdb.__version__}\n")
print(f"{'query':28} {'plain':>8} {'traced':>8} {'ratio':>6}")
for name, q in QUERIES.items():
    plain = best(lambda: con.execute(q).fetchall())
    traced = best(lambda: perche.trace(q, con))
    print(f"{name:28} {plain:7.3f}s {traced:7.3f}s {traced / plain:5.1f}x")
