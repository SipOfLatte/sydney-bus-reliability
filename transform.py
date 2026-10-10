"""Turn raw GTFS-Realtime trip update snapshots into Parquet stop events.

For every snapshot in data/raw/trip_updates we keep only the stops each bus is
about to reach (arrival within --window seconds of the snapshot). The last such
prediction before a bus reaches a stop is our best estimate of when it actually
arrived. Each raw file produces one Parquet file, so re-running only processes
snapshots that haven't been transformed yet.
"""

from __future__ import annotations

import argparse
import gzip
import time
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from google.transit import gtfs_realtime_pb2 as gtfs_rt

RAW_DIR = Path("data/raw/trip_updates")
OUT_DIR = Path("data/processed/stop_events")
DEFAULT_WINDOW_SECONDS = 150  # poll interval plus a little slack

TripDescriptor = gtfs_rt.TripDescriptor
StopTimeUpdate = gtfs_rt.TripUpdate.StopTimeUpdate
KEEP_TRIPS = {TripDescriptor.SCHEDULED, TripDescriptor.ADDED}  # ADDED = extra services not in the timetable
# NO_DATA stops are dropped. SKIPPED stops are kept and flagged, and the warehouse leaves them out.
KEEP_STOPS = {StopTimeUpdate.SCHEDULED, StopTimeUpdate.SKIPPED}

SCHEMA = pa.schema([
    ("snapshot_ts", pa.timestamp("s", tz="UTC")),
    ("trip_id", pa.string()),
    ("start_date", pa.string()),
    ("start_time", pa.string()),
    ("route_id", pa.string()),
    ("vehicle_id", pa.string()),
    ("stop_sequence", pa.int32()),
    ("stop_id", pa.string()),
    ("skipped", pa.bool_()),
    ("arrival_ts", pa.timestamp("s", tz="UTC")),
    ("arrival_delay_s", pa.int32()),
    ("departure_delay_s", pa.int32()),
])


def parse_snapshot(data: bytes, window: int) -> pa.Table:
    feed = gtfs_rt.FeedMessage()
    feed.ParseFromString(data)
    snapshot_ts = feed.header.timestamp  # when TfNSW built this feed
    cutoff = snapshot_ts + window

    # One list per column, turned into a table at the end
    rows = {name: [] for name in SCHEMA.names}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        if tu.trip.schedule_relationship not in KEEP_TRIPS:
            continue
        for stu in tu.stop_time_update:
            if stu.schedule_relationship not in KEEP_STOPS:
                continue
            if not stu.arrival.time or stu.arrival.time > cutoff:  # time 0 = missing
                continue
            rows["snapshot_ts"].append(snapshot_ts)
            rows["trip_id"].append(tu.trip.trip_id)
            rows["start_date"].append(tu.trip.start_date)
            rows["start_time"].append(tu.trip.start_time)
            rows["route_id"].append(tu.trip.route_id)
            rows["vehicle_id"].append(tu.vehicle.id or None)  # empty string means no vehicle assigned
            rows["stop_sequence"].append(stu.stop_sequence)
            rows["stop_id"].append(stu.stop_id)
            rows["skipped"].append(stu.schedule_relationship == StopTimeUpdate.SKIPPED)
            rows["arrival_ts"].append(stu.arrival.time)
            # A missing delay is stored as NULL so it can't be mistaken for "on schedule"
            rows["arrival_delay_s"].append(stu.arrival.delay if stu.arrival.HasField("delay") else None)
            rows["departure_delay_s"].append(stu.departure.delay if stu.departure.HasField("delay") else None)
    return pa.table(rows, schema=SCHEMA)


def output_path(raw_path: Path) -> Path:
    # data/raw/trip_updates/2026-10-09/054210Z.pb.gz -> .../date=2026-10-09/054210Z.parquet
    day = raw_path.parent.name
    return OUT_DIR / f"date={day}" / raw_path.name.replace(".pb.gz", ".parquet")


def run_once(window: int) -> int:
    processed = 0
    for raw_path in sorted(RAW_DIR.glob("*/*.pb.gz")):
        out = output_path(raw_path)
        if out.exists():  # already transformed on an earlier run
            continue
        try:
            table = parse_snapshot(gzip.decompress(raw_path.read_bytes()), window)
        except Exception as exc:  # one corrupt snapshot shouldn't stop the run
            print(f"Skipping {raw_path}: {exc}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename it, so a crash can't leave a half-written Parquet file
        # that the out.exists() check would then skip forever
        tmp = out.with_suffix(".tmp")
        pq.write_table(table, tmp)
        tmp.replace(out)
        processed += 1
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS,
                        help=f"keep stops reached within this many seconds (default: {DEFAULT_WINDOW_SECONDS})")
    parser.add_argument("--watch", type=int, metavar="SECONDS", default=0,
                        help="keep running, checking for new snapshots every SECONDS")
    args = parser.parse_args()

    try:
        while True:
            count = run_once(args.window)
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  transformed {count} new snapshot(s)", flush=True)
            if not args.watch:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
