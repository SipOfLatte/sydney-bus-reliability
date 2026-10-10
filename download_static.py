"""Download the Transport for NSW static bus timetable (GTFS) for route and stop names."""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:  # verify HTTPS with the macOS/Windows system trust store, like curl and browsers do
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

STATIC_URL = "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses"
STATIC_DIR = Path("data/static")
KEEP_FILES = ("routes.txt", "stops.txt", "trips.txt")


def main() -> int:
    api_key = os.environ.get("TFNSW_API_KEY", "").strip()
    if not api_key:
        sys.exit("set TFNSW_API_KEY to your Transport for NSW Open Data API key")

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = STATIC_DIR / "buses_gtfs.zip"
    request = Request(STATIC_URL, headers={"Authorization": f"apikey {api_key}"})
    print("Downloading static GTFS (this can take a minute)...")
    try:
        with urlopen(request, timeout=300) as response:  # the zip is about 100 MB
            zip_path.write_bytes(response.read())
    except HTTPError as exc:
        sys.exit(f"Transport for NSW API returned HTTP {exc.code}: {exc.read(500).decode(errors='replace')}")
    except URLError as exc:
        sys.exit(f"Could not reach Transport for NSW API: {exc.reason}")

    with zipfile.ZipFile(zip_path) as archive:
        # Only the tables the dashboard joins on; the rest of the timetable isn't needed
        for name in KEEP_FILES:
            if name in archive.namelist():
                archive.extract(name, STATIC_DIR)
                print(f"Extracted {STATIC_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
