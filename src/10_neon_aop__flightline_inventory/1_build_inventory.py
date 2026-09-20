"""Build the flight inventory: every DP1.30006.001 flightline NEON has, grouped by flight.

Writes, under DATA_ROOT/10_neon_aop__flightline_inventory/:

    availability.csv        one row per site-month, as the NEON API reports it
    flightlines.csv         one row per reflectance HDF5 file
    flights.csv             one row per flight (site x day): line count, size, release
    flights/<flight_id>.json  the manifest a CHTC job reads: the lines of that flight

Usage (from the repository root):

    uv run python src/10_neon_aop__flightline_inventory/1_build_inventory.py
    uv run python src/10_neon_aop__flightline_inventory/1_build_inventory.py --sites BLUE HEAL

About 375 API calls; a few minutes. Signed URLs are not stored — they expire in 7 days.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import asdict, fields
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[2]),
    str(Path(__file__).resolve().parents[1] / "00_common"),
]

import neon_api  # noqa: E402
from config.paths import FLIGHTS_DIR, INVENTORY_DIR  # noqa: E402
from project_env import neon_token  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", nargs="*", help="restrict to these site codes (default: all)")
    args = ap.parse_args()

    token = neon_token()
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    FLIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    availability = neon_api.product_availability(token)
    if args.sites:
        availability = [r for r in availability if r["site"] in set(args.sites)]
    _write_csv(INVENTORY_DIR / "availability.csv", availability, ["site", "month", "release"])
    print(f"{len(availability)} site-months to list")

    lines: list[neon_api.Flightline] = []
    for i, row in enumerate(availability, 1):
        files = neon_api.site_month_files(row["site"], row["month"], token)
        got = neon_api.flightlines_of(files)
        lines.extend(got)
        print(f"[{i}/{len(availability)}] {row['site']} {row['month']}: {len(got)} lines")

    # NEON lists a flight that covers two co-located sites (STEI/TREE, KONZ/UKFS, WOOD/DCFS,
    # CLBJ 2023) under both sites, so the same file appears twice; keep the first listing.
    seen: set[str] = set()
    lines = [x for x in lines if not (x.name in seen or seen.add(x.name))]

    line_rows = [{**asdict(x), "flight_id": x.flight_id} for x in lines]
    _write_csv(INVENTORY_DIR / "flightlines.csv", line_rows, [f.name for f in fields(neon_api.Flightline)] + ["flight_id"])

    by_flight: dict[str, list[neon_api.Flightline]] = defaultdict(list)
    for x in lines:
        by_flight[x.flight_id].append(x)

    flight_rows = []
    for flight_id, group in sorted(by_flight.items()):
        neon_api.write_flight_manifest(flight_id, group, FLIGHTS_DIR / f"{flight_id}.json")
        flight_rows.append(
            {
                "flight_id": flight_id,
                "site": group[0].site,
                "domain": group[0].domain,
                "date": group[0].date,
                "month": group[0].month,
                "release": group[0].release,
                "n_lines": len(group),
                "total_gb": round(sum(x.size for x in group) / 1e9, 2),
            }
        )
    _write_csv(INVENTORY_DIR / "flights.csv", flight_rows, list(flight_rows[0].keys()))

    total_tb = sum(r["total_gb"] for r in flight_rows) / 1000
    print(f"\n{len(lines)} flightlines in {len(flight_rows)} flights, {total_tb:.1f} TB of HDF5")
    print(f"manifests in {FLIGHTS_DIR}")


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
