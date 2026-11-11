import json
import random
from collections import Counter
from pathlib import Path

import pytest

import perche
from perche.cli import main
from test_oracle import generate, shop

EXAMPLE = Path(__file__).parent.parent / "examples" / "pipeline"


def test_example_pipeline():
    p = perche.Pipeline.from_dir(EXAMPLE).run()
    assert p.order == ["clean_orders", "revenue"]
    assert p["revenue"].rows[0][0] == "IT"
    assert p.roots("revenue", [0]) == {"../orders.csv": [0, 1, 4, 5, 6], "../customers.csv": [0, 1, 3]}

    tree = p.why("revenue", 1)
    assert tree["rows"] == [{"country": "FR", "orders": 1, "revenue": 42.0}]
    (clean,) = tree["from"]
    assert clean["step"] == "clean_orders" and [r["id"] for r in clean["rows"]] == [104]
    assert {c["source"]: [r["id"] for r in c["rows"]] for c in clean["from"]} == {
        "../orders.csv": [104],
        "../customers.csv": [3],
    }


def test_order_and_cycles():
    steps = {"report": "SELECT * FROM b", "b": "WITH x AS (SELECT 1 AS v) SELECT * FROM a, x", "a": "SELECT 1 AS k"}
    assert perche.Pipeline(steps).order == ["a", "b", "report"]
    with pytest.raises(ValueError, match="cycle"):
        perche.Pipeline({"a": "SELECT * FROM b", "b": "SELECT * FROM a"})


AFTER = [
    "SELECT count(*) AS n FROM step1",
    "SELECT DISTINCT * FROM step1",
    'SELECT "{c}", count(*) AS n FROM step1 GROUP BY 1',
    'SELECT * FROM step1 WHERE "{c}" IS NOT NULL',
    'SELECT a."{c}" FROM step1 a JOIN step1 b ON a."{c}" = b."{c}"',
]


def norm(lineage: dict) -> tuple:
    return tuple(sorted((k, tuple(v)) for k, v in lineage.items() if v))


@pytest.mark.parametrize("seed", range(150))
def test_pipeline_matches_inlined_query(seed):
    """Two steps traced separately must give the same lineage as one query with a CTE."""
    rnd = random.Random(seed)
    con = shop(seed % 7)
    first, _, _ = generate(rnd)
    col = perche.trace(first, con).columns[0]
    second = rnd.choice(AFTER).format(c=col)

    p = perche.Pipeline({"step1": first, "step2": second}, con).run()
    t = p["step2"]
    piped = Counter((repr(r), norm(p.roots("step2", [i]))) for i, r in enumerate(t.rows))

    inlined = perche.trace(f"WITH step1 AS ({first}) {second}", con)
    expected = Counter((repr(r), norm(lin)) for r, lin in zip(inlined.rows, inlined.lineage))
    assert piped == expected, (first, second)


def test_cli_run(capsys):
    assert main(["run", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "clean_orders" in out and "../orders.csv:5" in out

    assert main(["run", str(EXAMPLE), "--row", "0"]) == 0
    out = capsys.readouterr().out
    assert "← step clean_orders  (5 rows)" in out and out.count("999.99") == 4

    assert main(["run", str(EXAMPLE), "--row", "clean_orders:0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["step"] == "clean_orders"

    assert main(["run", str(EXAMPLE), "--row", "nope:0"]) == 2
