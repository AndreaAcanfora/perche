import random

import duckdb
import pytest


def make_shop(seed: int = 0, customers: int = 12, orders: int = 40, items: int = 70) -> duckdb.DuckDBPyConnection:
    """A small shop with NULLs and duplicates in the places that usually break things."""
    rnd = random.Random(seed)
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INT, name TEXT, country TEXT, tier INT)")
    con.execute("CREATE TABLE orders (id INT, customer_id INT, amount DECIMAL(8,2), status TEXT)")
    con.execute("CREATE TABLE items (order_id INT, sku TEXT, qty INT)")
    con.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)",
        [
            (i, f"c{i}", rnd.choice(["IT", "FR", "DE", None]), rnd.choice([1, 2, 3, None]))
            for i in range(1, customers + 1)
        ],
    )
    con.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?)",
        [
            (
                i,
                rnd.choice([*range(1, customers + 3), None]),  # some orphans
                rnd.choice([round(rnd.uniform(1, 200), 2), None]),
                rnd.choice(["paid", "paid", "refunded", "open"]),
            )
            for i in range(1, orders + 1)
        ],
    )
    con.executemany(
        "INSERT INTO items VALUES (?, ?, ?)",
        [(rnd.randint(1, orders + 2), rnd.choice("ABCDE"), rnd.randint(1, 5)) for _ in range(items)],
    )
    return con


@pytest.fixture
def shop():
    return make_shop()
