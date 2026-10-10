"""Write a fixed copy of the results for deployment (e.g. Streamlit Community Cloud).

Saves arrivals_enriched to data/export/arrivals.parquet and the snapshot count to
data/export/snapshots.txt. The dashboard reads these when there is no processed data,
so commit both files and push to update the deployed app.

Run with: python export.py
"""

from __future__ import annotations

import warehouse


def main() -> int:
    con = warehouse.connect()
    if not warehouse.is_live():
        raise SystemExit("No processed data to export. Run transform.py first.")
    warehouse.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = warehouse.EXPORT_ARRIVALS.with_suffix(".tmp")
    # Sorting by route puts similar rows next to each other, so the file compresses better
    con.execute(f"COPY (SELECT * FROM arrivals_enriched ORDER BY route_id, arrival_local) "
                f"TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)")
    tmp.replace(warehouse.EXPORT_ARRIVALS)
    snapshots, arrivals = con.execute(
        "SELECT (SELECT snapshots FROM collection), count(*) FROM arrivals_enriched"
    ).fetchone()
    warehouse.EXPORT_SNAPSHOTS.write_text(f"{snapshots}\n")
    size_mb = warehouse.EXPORT_ARRIVALS.stat().st_size / 1e6
    print(f"Exported {arrivals:,} arrivals from {snapshots:,} snapshots "
          f"to {warehouse.EXPORT_ARRIVALS.relative_to(warehouse.ROOT)} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
