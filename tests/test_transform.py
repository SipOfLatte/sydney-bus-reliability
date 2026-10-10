"""Tests for the transform (raw snapshot -> stop events) and warehouse (stop events -> arrivals).

Every test builds a tiny GTFS-Realtime feed in code, so no API key or collected data is needed.
Run with: python -m pytest
"""

from __future__ import annotations

import gzip
from datetime import datetime, timezone

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs_rt

import pipeline as tfnsw  # "pipeline" is taken by the fixture below
import quality
import transform
import warehouse

T0 = 1_791_500_000  # snapshot time (unix seconds)
TU = gtfs_rt.TripDescriptor
STU = gtfs_rt.TripUpdate.StopTimeUpdate


def make_feed(snapshot_ts: int, trips: list[dict]) -> bytes:
    """trips: [{"trip_id", "relationship"?, "stops": [(seq, stop_id, arrival_time, delay, relationship?)]}]"""
    feed = gtfs_rt.FeedMessage()
    feed.header.gtfs_realtime_version = "1.0"
    feed.header.timestamp = snapshot_ts
    for i, trip in enumerate(trips):
        tu = feed.entity.add(id=str(i)).trip_update
        tu.trip.trip_id = trip["trip_id"]
        tu.trip.route_id = "R1"
        tu.trip.start_date = "20261010"
        tu.trip.schedule_relationship = trip.get("relationship", TU.SCHEDULED)
        for seq, stop_id, arrival_time, delay, *rel in trip["stops"]:
            stu = tu.stop_time_update.add(stop_sequence=seq, stop_id=stop_id)
            stu.arrival.time = arrival_time
            stu.arrival.delay = delay
            stu.departure.delay = delay
            stu.schedule_relationship = rel[0] if rel else STU.SCHEDULED
    return feed.SerializeToString()


def kept_stops(data: bytes) -> list[tuple[str, int]]:
    table = transform.parse_snapshot(data, window=150).to_pydict()
    return list(zip(table["trip_id"], table["stop_sequence"]))


# --- Stage 1: transform.parse_snapshot -------------------------------------------------

def test_drops_no_data_cancelled_and_zero_timestamps():
    data = make_feed(T0, [
        {"trip_id": "ok", "stops": [
            (1, "A", T0 + 30, 10),                     # kept
            (2, "B", T0 + 60, 0, STU.NO_DATA),         # placeholder, no real prediction
            (3, "C", 0, 0),                            # broken timestamp (shows as 1970)
        ]},
        {"trip_id": "cancelled", "relationship": TU.CANCELED, "stops": [(1, "A", T0 + 30, 0)]},
    ])
    assert kept_stops(data) == [("ok", 1)]


def test_keeps_only_stops_reached_within_window():
    data = make_feed(T0, [{"trip_id": "t", "stops": [
        (1, "A", T0 - 20, 0),     # just passed: kept
        (2, "B", T0 + 150, 0),    # exactly at the window edge: kept
        (3, "C", T0 + 151, 0),    # beyond the window: dropped (a later snapshot will cover it)
        (4, "D", T0 + 900, 0),    # far future: dropped
    ]}])
    assert kept_stops(data) == [("t", 1), ("t", 2)]


# --- Stage 2: warehouse.arrivals -------------------------------------------------------

@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Point transform and warehouse at a temporary data folder with a minimal static timetable."""
    raw, out, static = tmp_path / "raw", tmp_path / "processed", tmp_path / "static"
    static.mkdir()
    (static / "routes.txt").write_text("route_id,route_short_name,route_long_name,route_desc\n"
                                       "R1,1,Test Route,Sydney Buses Network\n")
    (static / "trips.txt").write_text("trip_id,route_id,direction_id,route_direction\n"
                                      "t,R1,0,Outbound\nloop,R1,0,Loop\n")
    (static / "stops.txt").write_text("stop_id,stop_name,stop_lat,stop_lon\n"
                                      "A,Stop A,-33.8,151.2\nB,Stop B,-33.8,151.2\n")
    monkeypatch.setattr(transform, "RAW_DIR", raw)
    monkeypatch.setattr(transform, "OUT_DIR", out)
    monkeypatch.setattr(warehouse, "PROCESSED_GLOB", out / "*" / "*.parquet")
    monkeypatch.setattr(warehouse, "STATIC_DIR", static)

    def add_snapshot(data: bytes) -> None:
        ts = datetime.fromtimestamp(gtfs_rt.FeedMessage.FromString(data).header.timestamp, timezone.utc)
        path = raw / f"{ts:%Y-%m-%d}" / f"{ts:%H%M%S}Z.pb.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data))

    return add_snapshot


def arrivals() -> list[tuple]:
    return warehouse.connect().execute(
        "SELECT trip_id, stop_sequence, arrival_delay_s, status FROM arrivals ORDER BY trip_id, stop_sequence"
    ).fetchall()


def test_latest_snapshot_wins_when_a_stop_appears_twice(pipeline):
    # The bus is "about to arrive" in two snapshots; it was held up, so the delay grew.
    pipeline(make_feed(T0, [{"trip_id": "t", "stops": [(5, "A", T0 + 90, 120)]}]))
    pipeline(make_feed(T0 + 120, [{"trip_id": "t", "stops": [(5, "A", T0 + 200, 400)]}]))
    transform.run_once(window=150)

    assert arrivals() == [("t", 5, 400, "Late")]


def test_loop_route_visiting_same_stop_twice_counts_as_two_arrivals(pipeline):
    pipeline(make_feed(T0, [{"trip_id": "loop", "stops": [
        (1, "A", T0 + 10, 0),
        (2, "B", T0 + 60, 0),
        (3, "A", T0 + 120, 0),   # back at stop A: a separate arrival
    ]}]))
    transform.run_once(window=150)

    assert [(trip, seq) for trip, seq, *_ in arrivals()] == [("loop", 1), ("loop", 2), ("loop", 3)]


def test_rerunning_transform_is_idempotent(pipeline):
    pipeline(make_feed(T0, [{"trip_id": "t", "stops": [(1, "A", T0 + 30, 0)]}]))

    assert transform.run_once(window=150) == 1
    first = arrivals()
    assert transform.run_once(window=150) == 0   # nothing new to process
    assert arrivals() == first                   # and no duplicates


def test_find_gaps_reports_only_long_stretches():
    t = [datetime(2026, 10, 10, 0, m, tzinfo=timezone.utc) for m in (0, 2, 4, 14, 16)]
    assert quality.find_gaps(t, max_gap=300) == [(t[2], t[3])]


def test_download_stalling_midway_raises_api_error(monkeypatch):
    # The poller only catches TfnswApiError, so anything else would stop collection
    class StalledResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(tfnsw, "urlopen", lambda *a, **k: StalledResponse())
    with pytest.raises(tfnsw.TfnswApiError):
        tfnsw.fetch_live_bus_feed("key")
