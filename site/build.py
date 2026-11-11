"""Build the browser demo into site/dist.

Downloads one month of NYC yellow taxi trips from the TLC, keeps the trips
picked up before 2024-01-08 (which includes the ones wrongly dated 2002,
2009 and 2023), and precomputes the traced SQL of the preset charts so the
page works before Pyodide has loaded.

    python site/build.py [--cache DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path

import duckdb

from perche.rewrite import rewrite

HERE = Path(__file__).parent
TRIPS_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet"
ZONES_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
TABLES = {"trips", "zones"}

PRESETS = [
    {
        "id": "days",
        "title": "Trips per day",
        "blurb": "A week of January 2024. So why are there trips in 2002, 2009 and 2023? Click a bar.",
        "sql": """SELECT strftime(pickup, '%Y-%m-%d') AS day, count(*) AS trips
FROM trips
GROUP BY day
ORDER BY day""",
        "sort": "pickup",
    },
    {
        "id": "longest",
        "title": "Longest trip per borough",
        "blurb": "Manhattan's longest ride is more than twice around the Earth. Click it: the trips are sorted by distance.",
        "sql": """SELECT z.borough, max(t.miles) AS longest_trip_miles
FROM trips t
JOIN zones z ON z.id = t.pickup_zone
GROUP BY z.borough
ORDER BY longest_trip_miles DESC""",
        "sort": "miles DESC",
    },
    {
        "id": "payment",
        "title": "Revenue by payment type",
        "blurb": "The TLC data dictionary defines payment types 1 to 6. Who paid $764k with type 0?",
        "sql": """SELECT CASE payment_type
         WHEN 0 THEN '0 (undocumented)' WHEN 1 THEN '1 credit card'
         WHEN 2 THEN '2 cash' WHEN 3 THEN '3 no charge'
         WHEN 4 THEN '4 dispute' ELSE payment_type::TEXT END AS payment,
       round(sum(total)) AS revenue
FROM trips
GROUP BY payment
ORDER BY revenue DESC""",
        "sort": "total DESC",
    },
    {
        "id": "refunds",
        "title": "Negative fares by pickup zone",
        "blurb": "Thousands of trips have a negative total. A join, a filter and a top 10: every bar still knows its trips.",
        "sql": """SELECT z.name AS zone, count(*) AS negative_trips
FROM trips t
JOIN zones z ON z.id = t.pickup_zone
WHERE t.total < 0
GROUP BY z.name
ORDER BY negative_trips DESC
LIMIT 10""",
        "sort": "total",
    },
]


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"downloading {url}")
        with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(HERE / ".cache"))
    ap.add_argument("--out", default=str(HERE / "dist"))
    args = ap.parse_args()
    cache, out = Path(args.cache), Path(args.out)
    cache.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    month = fetch(TRIPS_URL, cache / "yellow_tripdata_2024-01.parquet")
    zones = fetch(ZONES_URL, cache / "taxi_zone_lookup.csv")

    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT tpep_pickup_datetime AS pickup, tpep_dropoff_datetime AS dropoff,
                   passenger_count AS passengers, trip_distance AS miles,
                   PULocationID AS pickup_zone, DOLocationID AS dropoff_zone, payment_type,
                   fare_amount AS fare, tip_amount AS tip, total_amount AS total
            FROM '{month}' WHERE tpep_pickup_datetime < '2024-01-08'
        ) TO '{out / "trips.parquet"}' (FORMAT parquet, COMPRESSION zstd)"""
    )
    con.execute(
        f"""COPY (SELECT LocationID AS id, Borough AS borough, Zone AS name FROM '{zones}' ORDER BY id)
            TO '{out / "zones.csv"}' (HEADER)"""
    )

    # Same tables as the page creates, to check every preset runs.
    con.execute(f"CREATE TABLE trips AS SELECT * FROM '{out / 'trips.parquet'}'")
    con.execute(f"CREATE TABLE zones AS SELECT * FROM '{out / 'zones.csv'}'")
    presets = []
    for p in PRESETS:
        traced = rewrite(p["sql"], TABLES)
        rows = con.execute(traced["sql"]).fetchall()
        print(f"{p['id']:10} {len(rows):3} rows  e.g. {rows[0][:2]}")
        presets.append({**p, "traced": traced})
    meta = {"trips": con.execute("SELECT count(*) FROM trips").fetchone()[0]}

    (out / "presets.json").write_text(json.dumps({"presets": presets, **meta}, indent=1))
    shutil.copy(HERE.parent / "src" / "perche" / "rewrite.py", out / "rewrite.py")
    for f in ("index.html", "app.js"):
        shutil.copy(HERE / f, out / f)
    print(f"built {out}")


if __name__ == "__main__":
    main()
