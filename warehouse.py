"""DuckDB views over the processed Parquet files, shared by analysis and the dashboard.

Views:
    stop_events  every kept row from every snapshot (one stop can appear several times)
    arrivals     one row per trip per stop: the latest prediction, i.e. the observed arrival
    routes, trips, stops   the static timetable (run download_static.py first)
    arrivals_enriched      arrivals joined to route, direction and stop names
    collection             how many snapshots the data came from

If there is no processed data (e.g. on Streamlit Community Cloud), the views are built
from the fixed copy written by export.py instead. Set BUS_DATA=export to force that locally.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
PROCESSED_GLOB = ROOT / "data/processed/stop_events/*/*.parquet"
STATIC_DIR = ROOT / "data/static"
EXPORT_DIR = ROOT / "data/export"
EXPORT_ARRIVALS = EXPORT_DIR / "arrivals.parquet"
EXPORT_SNAPSHOTS = EXPORT_DIR / "snapshots.txt"
TIMEZONE = "Australia/Sydney"

# On time = no more than 1 minute early and no more than 5 minutes late.
# Anything outside that window is classed as Early or Late.
ON_TIME_EARLY_S = -60
ON_TIME_LATE_S = 300


def is_live() -> bool:
    """True when reading the processed snapshots, False when reading the exported copy."""
    if os.environ.get("BUS_DATA") == "export":
        return False
    return any(PROCESSED_GLOB.parent.parent.glob("*/*.parquet"))


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    if not is_live():
        con.execute(f"CREATE VIEW arrivals_enriched AS SELECT * FROM read_parquet('{EXPORT_ARRIVALS}')")
        con.execute("CREATE VIEW arrivals AS SELECT * FROM arrivals_enriched")
        snapshots = int(EXPORT_SNAPSHOTS.read_text()) if EXPORT_SNAPSHOTS.exists() else 0
        con.execute(f"CREATE VIEW collection AS SELECT {snapshots} AS snapshots")
        return con
    con.execute(f"""
        CREATE VIEW stop_events AS
        -- hive_partitioning turns the date=... folder names into a column; arrival_ts already has the day
        SELECT * EXCLUDE (date) FROM read_parquet('{PROCESSED_GLOB}', hive_partitioning = true)
    """)
    con.execute(f"""
        CREATE VIEW arrivals AS
        SELECT
            trip_id, start_date, route_id, stop_id, stop_sequence,
            timezone('{TIMEZONE}', arrival_ts) AS arrival_local,
            arrival_delay_s,
            CASE
                WHEN arrival_delay_s < {ON_TIME_EARLY_S} THEN 'Early'
                WHEN arrival_delay_s > {ON_TIME_LATE_S} THEN 'Late'
                ELSE 'On time'
            END AS status
        FROM stop_events
        WHERE NOT skipped AND arrival_delay_s IS NOT NULL
        -- Several snapshots can see the same stop. Keep the newest, made closest to the arrival.
        -- Partitioning on stop_sequence means a loop route that visits a stop twice gives two arrivals.
        QUALIFY row_number() OVER (
            PARTITION BY trip_id, start_date, stop_sequence ORDER BY snapshot_ts DESC
        ) = 1
    """)
    con.execute("CREATE VIEW collection AS SELECT count(DISTINCT snapshot_ts) AS snapshots FROM stop_events")
    # all_varchar keeps IDs as text, so values with leading zeros match the realtime feed
    for name in ("routes", "trips", "stops"):
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_csv('{STATIC_DIR / name}.txt', all_varchar = true)")
    con.execute("""
        CREATE VIEW arrivals_enriched AS
        SELECT
            a.*,
            r.route_short_name,
            r.route_short_name || ' · ' || r.route_long_name AS route_label,
            -- TfNSW leaves some routes without a network; give them a clearer label
            replace(r.route_desc, 'Unknown Description', 'Unclassified') AS network,
            -- Weekend = Saturday/Sunday by the bus's arrival time in Sydney (public holidays not handled)
            CASE WHEN dayofweek(a.arrival_local) IN (0, 6) THEN 'Weekend' ELSE 'Weekday' END AS day_type,
            t.route_direction,
            s.stop_name
        FROM arrivals a
        -- Left joins keep arrivals whose trip or stop is missing from the timetable
        LEFT JOIN routes r USING (route_id)
        LEFT JOIN trips t USING (trip_id)
        LEFT JOIN stops s USING (stop_id)
    """)
    return con
