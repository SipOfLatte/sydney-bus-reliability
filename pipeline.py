"""Fetch the Transport for NSW live bus GTFS-Realtime feed."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:  # verify HTTPS with the macOS/Windows system trust store, like curl and browsers do
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

FEED_URL = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"
DEFAULT_OUTPUT = Path("data/nsw_gtfs_realtime_buses.pb")
DEFAULT_TIMEOUT_SECONDS = 30


class TfnswApiError(RuntimeError):
    """Raised when the Transport for NSW API request fails."""


def fetch_live_bus_feed(
    api_key: str, timeout: int = DEFAULT_TIMEOUT_SECONDS, url: str = FEED_URL
) -> bytes:
    """Return the current GTFS-Realtime bus feed as protobuf bytes."""
    if not api_key.strip():
        raise ValueError("The Transport for NSW API key must not be empty.")

    request = Request(
        url,
        headers={
            # TfNSW wants the word "apikey" in front of the key
            "Authorization": f"apikey {api_key.strip()}",
            "Accept": "application/x-google-protobuf",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        # The response body usually says what went wrong (bad key, rate limit), so show the start of it
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
        message = f"Transport for NSW API returned HTTP {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise TfnswApiError(message) from exc
    except URLError as exc:
        raise TfnswApiError(f"Could not reach Transport for NSW API: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to save the protobuf feed (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args()

    api_key = os.environ.get("TFNSW_API_KEY")
    if not api_key:
        parser.error("set TFNSW_API_KEY to your Transport for NSW Open Data API key")
    if args.timeout <= 0:
        parser.error("--timeout must be a positive number")

    try:
        feed = fetch_live_bus_feed(api_key, timeout=args.timeout)
    except (TfnswApiError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(feed)
    print(f"Saved {len(feed)} bytes to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())