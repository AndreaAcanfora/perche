import duckdb
import pytest

import perche


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE c AS SELECT * FROM (VALUES (1, 'Ada', 'IT'), (2, 'Bob', 'FR'), (3, 'Cy', 'IT')) v(id, name, country)")
    c.execute("CREATE TABLE o AS SELECT * FROM (VALUES (10, 1, 5), (11, 1, 7), (12, 2, 1), (13, 3, 2)) v(id, cust, amount)")
    return c


def run(con, sql):
    t = perche.trace(sql, con)
    return list(zip(t.rows, t.lineage))


def test_filter(con):
    assert run(con, "SELECT name FROM c WHERE country = 'IT'") == [(("Ada",), {"c": [0]}), (("Cy",), {"c": [2]})]


def test_star_hides_lineage_columns(con):
    t = perche.trace("SELECT * FROM c JOIN o ON o.cust = c.id", con)
    assert t.columns == ["id", "name", "country", "id", "cust", "amount"]
    t = perche.trace("SELECT o.* FROM c JOIN o ON o.cust = c.id WHERE c.id = 2", con)
    assert t.columns == ["id", "cust", "amount"]
    assert t.lineage == [{"c": [1], "o": [2]}]


def test_join_group_by(con):
    rows = run(con, "SELECT c.country, sum(o.amount) FROM c JOIN o ON o.cust = c.id GROUP BY 1 ORDER BY 1")
    assert rows == [(("FR", 1), {"c": [1], "o": [2]}), (("IT", 14), {"c": [0, 2], "o": [0, 1, 3]})]


def test_global_aggregate_over_nothing(con):
    assert run(con, "SELECT count(*) FROM c WHERE false") == [((0,), {"c": []})]


def test_having(con):
    rows = run(con, "SELECT cust, count(*) n FROM o GROUP BY cust HAVING count(*) > 1")
    assert rows == [((1, 2), {"o": [0, 1]})]


def test_distinct(con):
    rows = run(con, "SELECT DISTINCT country FROM c ORDER BY country")
    assert rows == [(("FR",), {"c": [1]}), (("IT",), {"c": [0, 2]})]


def test_left_join_unmatched_side_is_empty(con):
    rows = run(con, "SELECT c.name, o.id FROM c LEFT JOIN o ON o.cust = c.id AND o.amount > 6 ORDER BY c.id")
    assert rows == [
        (("Ada", 11), {"c": [0], "o": [1]}),
        (("Bob", None), {"c": [1], "o": []}),
        (("Cy", None), {"c": [2], "o": []}),
    ]


def test_self_join_merges_per_table(con):
    rows = run(con, "SELECT a.id, b.id FROM o a JOIN o b ON a.cust = b.cust AND a.id < b.id")
    assert rows == [((10, 11), {"o": [0, 1]})]


def test_cte_used_twice(con):
    sql = """
        WITH big AS (SELECT * FROM o WHERE amount > 1)
        SELECT x.id, y.id FROM big x JOIN big y ON x.cust = y.cust AND x.id < y.id
    """
    assert run(con, sql) == [((10, 11), {"o": [0, 1]})]


def test_chained_ctes(con):
    sql = """
        WITH per_cust AS (SELECT cust, sum(amount) AS total FROM o GROUP BY cust),
             top AS (SELECT * FROM per_cust WHERE total > 1)
        SELECT c.name, top.total FROM top JOIN c ON c.id = top.cust ORDER BY c.name
    """
    assert run(con, sql) == [
        (("Ada", 12), {"o": [0, 1], "c": [0]}),
        (("Cy", 2), {"o": [3], "c": [2]}),
    ]


def test_cte_still_readable_from_where_subquery(con):
    sql = "WITH it AS (SELECT * FROM c WHERE country = 'IT') SELECT id FROM o WHERE cust IN (SELECT id FROM it)"
    assert [r for r, _ in run(con, sql)] == [(10,), (11,), (13,)]


def test_subquery_in_from(con):
    sql = "SELECT s.total FROM (SELECT cust, sum(amount) total FROM o GROUP BY cust) s WHERE s.cust = 1"
    assert run(con, sql) == [((12,), {"o": [0, 1]})]


def test_union_all_and_union(con):
    sql = "SELECT country FROM c WHERE id = 1 UNION ALL SELECT 'FR'"
    assert run(con, sql) == [(("IT",), {"c": [0]}), (("FR",), {"c": []})]

    sql = "SELECT country AS k FROM c UNION SELECT CAST(cust AS TEXT) FROM o WHERE cust = 2 ORDER BY k"
    assert run(con, sql) == [
        (("2",), {"c": [], "o": [2]}),
        (("FR",), {"c": [1], "o": []}),
        (("IT",), {"c": [0, 2], "o": []}),
    ]


def test_window_functions_keep_row_lineage(con):
    rows = run(con, "SELECT id, sum(amount) OVER (PARTITION BY cust) FROM o WHERE cust = 1 ORDER BY id")
    assert rows == [((10, 12), {"o": [0]}), ((11, 12), {"o": [1]})]


def test_views_and_files_are_copied(con, tmp_path):
    con.execute("CREATE VIEW it AS SELECT * FROM c WHERE country = 'IT'")
    path = tmp_path / "prices.csv"
    path.write_text("sku,price\nA,10\nB,20\nA,30\n")
    sql = f"SELECT it.name, prices.sku FROM it, '{path}' JOIN o ON true WHERE o.id = 10 AND it.id = 3 AND prices.price > 15"
    t = perche.trace(sql, con)
    assert t.rows == [("Cy", "B"), ("Cy", "A")]
    assert t.lineage == [
        {"it": [1], str(path): [1], "o": [0]},
        {"it": [1], str(path): [2], "o": [0]},
    ]
    assert t.why(1)[str(path)] == [{"rowid": 2, "sku": "A", "price": 30}]


def test_table_function(con, tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a\n1\n2\n3\n")
    t = perche.trace(f"SELECT sum(a) FROM read_csv('{path}') WHERE a > 1", con)
    assert t.rows == [(5,)]
    assert list(t.lineage[0].values()) == [[1, 2]]


def test_why_returns_source_rows(con):
    t = perche.trace("SELECT c.country, count(*) FROM c JOIN o ON o.cust = c.id GROUP BY 1 ORDER BY 1", con)
    assert t.why(0) == {
        "c": [{"rowid": 1, "id": 2, "name": "Bob", "country": "FR"}],
        "o": [{"rowid": 2, "id": 12, "cust": 2, "amount": 1}],
    }


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT cust FROM o INTERSECT ALL SELECT id FROM c",
        "SELECT cust FROM o EXCEPT ALL SELECT id FROM c",
        "SELECT DISTINCT ON (country) name FROM c",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
        "INSERT INTO c VALUES (4, 'D', 'DE')",
    ],
)
def test_unsupported(con, sql):
    with pytest.raises(perche.UnsupportedQuery):
        perche.trace(sql, con)


def test_repeated_traces_and_lazy_api(con, tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a\n1\n2\n")
    sql = f"SELECT c.country, count(*) FROM c, '{path}' GROUP BY 1 ORDER BY 1"
    first, second = perche.trace(sql, con), perche.trace(sql, con)  # temp names must not clash
    assert first.lineage == second.lineage
    assert second.lineage_of(1) == {"c": [0, 2], str(path): [0, 1]}
    assert second.counts(1) == [{"c": 1, str(path): 2}]
    with pytest.raises(IndexError):
        second.lineage_of(2)
    with perche.trace("SELECT 1 AS one", con) as t:
        assert (t.rows, t.lineage, t.why(0)) == ([(1,)], [{}], {})


def test_duplicate_column_names(con):
    t = perche.trace("SELECT c.id, o.id FROM c JOIN o ON o.cust = c.id WHERE o.id = 12", con)
    assert (t.columns, t.rows) == (["id", "id"], [(2, 12)])


def test_intersect(con):
    sql = "SELECT cust FROM o INTERSECT SELECT id FROM c WHERE country = 'IT' ORDER BY 1"
    assert run(con, sql) == [((1,), {"o": [0, 1], "c": [0]}), ((3,), {"o": [3], "c": [2]})]


def test_except_keeps_only_left_lineage(con):
    sql = "SELECT id FROM c EXCEPT SELECT cust FROM o WHERE amount > 4"
    assert sorted(run(con, sql), key=repr) == [((2,), {"c": [1], "o": []}), ((3,), {"c": [2], "o": []})]


def test_set_operations_nest(con):
    sql = "(SELECT country FROM c EXCEPT SELECT 'FR') UNION ALL (SELECT 'X' INTERSECT SELECT 'X')"
    assert sorted(run(con, sql), key=repr) == [(("IT",), {"c": [0, 2]}), (("X",), {"c": []})]
