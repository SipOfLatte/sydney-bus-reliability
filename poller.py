"""Poll the Transport for NSW bus GTFS-Realtime feeds and archive each snapshot.

Snapshots are saved gzipped as data/raw/<feed>/<YYYY-MM-DD>/<HHMMSS>Z.pb.gz (UTC).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline import DEFAULT_TIMEOUT_SECONDS, FEED_URL, TfnswApiError, fetch_live_bus_feed

FEEDS = {
    "trip_updates": FEED_URL,
    "vehicle_positions": "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses",
}
DEFAULT_RAW_DIR = Path("data/raw")
DEFAULT_INTERVAL_SECONDS = 120
DEFAULT_HOURS = 48
MAX_BACKOFF_SECONDS = 600


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}", flush=True)


def save_snapshot(raw_dir: Path, feed: str, data: bytes, fetched_at: datetime) -> Path:
    path = raw_dir / feed / f"{fetched_at:%Y-%m-%d}" / f"{fetched_at:%H%M%S}Z.pb.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(data))
    tmp.replace(path)  # never leave a half-written file behind if interrupted
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                        help=f"seconds between polls (default: {DEFAULT_INTERVAL_SECONDS})")
    parser.add_argument("--feeds", nargs="+", choices=FEEDS, default=list(FEEDS),
                        help="which feeds to poll (default: all)")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR,
                        help=f"where to store snapshots (default: {DEFAULT_RAW_DIR})")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                        help=f"stop after this many hours, 0 = run until Ctrl+C (default: {DEFAULT_HOURS})")
    parser.add_argument("--max-polls", type=int, default=0,
                        help="stop after this many polls (default: run until Ctrl+C)")
    args = parser.parse_args()

    api_key = os.environ.get("TFNSW_API_KEY")
    if not api_key:
        parser.error("set TFNSW_API_KEY to your Transport for NSW Open Data API key")
    if args.interval < 15:
        parser.error("--interval must be at least 15 seconds")

    last_hash: dict[str, str] = {}  # hash of the last snapshot saved, per feed
    failures = 0
    polls = 0
    deadline = time.time() + args.hours * 3600 if args.hours else None  # None = no time limit
    log(f"Polling {', '.join(args.feeds)} every {args.interval}s into {args.raw_dir}"
        + (f" for {args.hours:g}h" if deadline else ""))

    try:
        while True:
            # monotonic() ignores clock changes, so the sleep below stays correct
            started = time.monotonic()
            had_error = False

            for feed in args.feeds:
                # UTC file names sort in order and skip the daylight saving jumps
                fetched_at = datetime.now(timezone.utc)
                try:
                    data = fetch_live_bus_feed(api_key, DEFAULT_TIMEOUT_SECONDS, FEEDS[feed])
                except TfnswApiError as exc:
                    had_error = True
                    log(f"{feed}: {exc}")
                    continue

                # The feed only refreshes every so often; skip exact repeats.
                digest = hashlib.sha256(data).hexdigest()
                if last_hash.get(feed) == digest:
                    log(f"{feed}: unchanged, skipped")
                    continue
                last_hash[feed] = digest

                path = save_snapshot(args.raw_dir, feed, data, fetched_at)
                log(f"{feed}: saved {len(data) / 1e6:.1f} MB -> {path}")

            polls += 1
            if args.max_polls and polls >= args.max_polls:
                break
            if deadline and time.time() >= deadline:
                log(f"Reached {args.hours:g}h limit.")
                break

            # Back off exponentially on repeated failures (outage, rate limit).
            failures = failures + 1 if had_error else 0
            wait = min(args.interval * 2 ** failures, MAX_BACKOFF_SECONDS) if failures else args.interval
            # Subtract the time spent fetching so polls stay on a steady schedule
            time.sleep(max(0.0, wait - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
