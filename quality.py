"""Data-quality report: how complete the collection is and how far to trust the arrival times.

Checks:
    gaps        stretches where the poller saved no snapshot for longer than --max-gap seconds
    lead time   how long before the bus reached the stop its last prediction was made
    shift       how much the predicted arrival moved between the first and last snapshot that saw it
    cut-offs    early % vs late % under the official window and under even cut-offs

Run with: python quality.py
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import warehouse

RAW_DIR = warehouse.ROOT / "data/raw"
SYDNEY = ZoneInfo(warehouse.TIMEZONE)
DEFAULT_MAX_GAP_SECONDS = 300  # poll interval is 120s, so this allows one missed poll


def snapshot_times(feed_dir: Path) -> list[datetime]:
    # data/raw/trip_updates/2026-10-09/054210Z.pb.gz -> 2026-10-09 05:42:10 UTC
    return sorted(
        datetime.strptime(f"{p.parent.name} {p.name[:6]}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)
        for p in feed_dir.glob("*/*.pb.gz")
    )


def find_gaps(times: list[datetime], max_gap: int) -> list[tuple[datetime, datetime]]:
    return [(a, b) for a, b in zip(times, times[1:]) if (b - a).total_seconds() > max_gap]


def report_gaps(max_gap: int) -> None:
    print(f"## Snapshot gaps (longer than {max_gap}s)\n")
    for feed_dir in sorted(p for p in RAW_DIR.iterdir() if p.is_dir()):
        times = snapshot_times(feed_dir)
        if not times:
            continue
        gaps = find_gaps(times, max_gap)
        missing = sum((b - a).total_seconds() for a, b in gaps) / 3600
        span = (times[-1] - times[0]).total_seconds() / 3600
        print(f"{feed_dir.name}: {len(times)} snapshots over {span:.1f}h, "
              f"{len(gaps)} gap(s) totalling {missing:.1f}h")
        for a, b in gaps:
            a_local = a.astimezone(SYDNEY)
            b_local = b.astimezone(SYDNEY)
            print(f"  {a_local:%a %d %b %H:%M} -> {b_local:%H:%M}  ({(b - a).total_seconds() / 60:.0f} min)")
    print()


def report_predictions(con) -> None:
    row = con.sql("""
        WITH per_stop AS (
            SELECT
                count(*) AS n_snapshots,
                -- seconds between the last snapshot that saw this stop and its predicted arrival
                epoch(arg_max(arrival_ts, snapshot_ts)) - epoch(max(snapshot_ts)) AS lead_s,
                abs(epoch(arg_max(arrival_ts, snapshot_ts)) - epoch(arg_min(arrival_ts, snapshot_ts))) AS shift_s
            FROM stop_events
            WHERE NOT skipped
            GROUP BY trip_id, start_date, stop_sequence
        )
        SELECT
            count(*),
            avg((n_snapshots > 1)::INT),
            median(lead_s),
            quantile_cont(lead_s, 0.9),
            avg((lead_s < 0)::INT),
            median(shift_s) FILTER (WHERE n_snapshots > 1),
            quantile_cont(shift_s, 0.9) FILTER (WHERE n_snapshots > 1),
            avg((shift_s > 60)::INT) FILTER (WHERE n_snapshots > 1)
        FROM per_stop
    """).fetchone()
    stops, multi, lead_med, lead_p90, past, shift_med, shift_p90, shift_over_60 = row

    print("## Prediction quality\n")
    print(f"Bus-stop arrivals: {stops:,}")
    print(f"Lead time of the last prediction: median {lead_med:.0f}s, 90th percentile {lead_p90:.0f}s")
    print(f"Last prediction already in the past (feed reporting the actual arrival): {past:.1%}")
    print(f"Seen in 2+ snapshots: {multi:.1%}; of those, prediction moved by median {shift_med:.0f}s, "
          f"90th percentile {shift_p90:.0f}s, more than 1 min for {shift_over_60:.1%}")


def report_cutoffs(con) -> None:
    print("\n## Cut-off sensitivity\n")
    print("| Window | Early | On time | Late | Late ÷ early |")
    print("|---|---|---|---|---|")
    windows = [("Official (1 min early / 5 min late)", warehouse.ON_TIME_EARLY_S, warehouse.ON_TIME_LATE_S)]
    windows += [(f"Even ±{m} min", -60 * m, 60 * m) for m in (1, 2, 3, 5)]
    for label, early_s, late_s in windows:
        early, late = con.execute(
            "SELECT avg((arrival_delay_s < ?)::INT), avg((arrival_delay_s > ?)::INT) FROM arrivals",
            [early_s, late_s],
        ).fetchone()
        print(f"| {label} | {early:.1%} | {1 - early - late:.1%} | {late:.1%} | {late / early:.1f}× |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-gap", type=int, default=DEFAULT_MAX_GAP_SECONDS,
                        help=f"report gaps between snapshots longer than this (default: {DEFAULT_MAX_GAP_SECONDS})")
    args = parser.parse_args()
    report_gaps(args.max_gap)
    con = warehouse.connect()
    report_predictions(con)
    report_cutoffs(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
