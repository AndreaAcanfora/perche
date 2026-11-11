import json
from pathlib import Path

from perche.cli import main

EXAMPLES = Path(__file__).parent.parent / "examples"
QUERY = (
    f"SELECT c.country, sum(o.amount) AS revenue FROM '{EXAMPLES}/orders.csv' o "
    f"JOIN '{EXAMPLES}/customers.csv' c ON c.id = o.customer_id "
    "WHERE o.status = 'paid' GROUP BY c.country ORDER BY revenue DESC"
)


def test_table(capsys):
    assert main([QUERY]) == 0
    out = capsys.readouterr().out
    assert "2212.78" in out
    assert "orders.csv:5" in out and "customers.csv:3" in out


def test_row(capsys):
    assert main([QUERY, "--row", "0"]) == 0
    out = capsys.readouterr().out
    assert out.count("999.99") == 2  # the duplicated order shows up
    assert "Dario" in out


def test_row_json(capsys):
    assert main([QUERY, "--row", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["row"] == {"country": "FR", "revenue": 42.0}
    assert [r["name"] for r in data["sources"][f"{EXAMPLES}/customers.csv"]] == ["Chloé"]


def test_errors(capsys):
    assert main([QUERY, "--row", "9"]) == 2
    assert main(["SELECT a FROM t INTERSECT SELECT a FROM t"]) == 2
    assert "INTERSECT" in capsys.readouterr().err


def test_db_file(tmp_path, capsys):
    import duckdb

    db = tmp_path / "shop.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE t AS SELECT range AS x FROM range(5)")
    assert main(["SELECT sum(x) FROM t WHERE x > 2", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"row": {"sum(x)": 7}, "lineage": {"t": [3, 4]}}]
