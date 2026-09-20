"""What is on the share, per flight, in which format: the ledger.

Runs on the Windows machine (the share is G:), standard library only. For every flight
folder under 30_neon_aop__trait_maps/ it counts the trait-map tarballs
against the flight manifest, reads the loose `<line>_traits.json` sidecars (written next
to the tarballs since format 2026-09-17.2) for the processing and format versions, and
classifies older lines by the tarball's write time against the format history in
`src/30_neon_aop__trait_maps/1_map_traits_line.py`. Also notes whether the flight's
samples + coefficients were archived (20_neon_aop__hytools_correction/<flight>/).

    uv run python chtc/ledger.py                 # summary + writes the CSV
    uv run python chtc/ledger.py --stale         # only flights with lines to redo
    uv run python chtc/ledger.py --year 2019

Writes ENSPEC_PROJECT_DIR/ledger/flights_ledger.csv (one row per flight) and
lines_ledger.csv (one row per line) so any session, on any machine with G:, can see
the state without CHTC access.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config.paths import ENSPEC_PROJECT_DIR, FLIGHTS_DIR  # noqa: E402

#: Current format versions, as written into the sidecars by the two stage scripts. Add a
#: version to TRAIT_MAP_FORMATS when a change leaves the rasters usable (the 2026-09-19.1
#: quicklook did), so that older lines are not flagged for a rerun.
TRAIT_MAP_FORMAT = "2026-09-19.1"
TRAIT_MAP_FORMATS = ("2026-09-19.1",)
#: Lines with no loose sidecar are classified by write time (lab server clock, local):
#: before this the QA raster was uint16 single-band without ATCOR flags.
FORMAT_2_DEPLOYED = datetime(2026, 9, 17, 22, 11)

TRAITS = ENSPEC_PROJECT_DIR / "30_neon_aop__trait_maps"
CORRECTION = ENSPEC_PROJECT_DIR / "20_neon_aop__hytools_correction"
LEDGER_DIR = ENSPEC_PROJECT_DIR / "ledger"


def line_row(flight: str, stem: str) -> dict:
    tar = TRAITS / flight / f"traits_{stem}.tar"
    side = TRAITS / flight / f"{stem}_traits.json"
    row = {"flight_id": flight, "line": stem, "traits_tar_mb": "", "written": "",
           "processing_version": "", "format_version": "", "seconds": "", "status": "missing"}
    if not tar.exists():
        return row
    st = tar.stat()
    row["traits_tar_mb"] = round(st.st_size / 1e6, 1)
    row["written"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    if side.exists():
        j = json.loads(side.read_text())
        row["processing_version"] = j.get("processing_version", "")
        row["format_version"] = j.get("format_version", "")
        row["seconds"] = j.get("seconds", "")
    else:
        row["format_version"] = "2026-09-17.1" if datetime.fromtimestamp(st.st_mtime) < FORMAT_2_DEPLOYED else "2026-09-17.2?"
    if row["format_version"] in TRAIT_MAP_FORMATS or row["format_version"] == "2026-09-17.2?":
        row["status"] = "current"  # "?" = classified by write time, no loose sidecar yet
    else:
        row["status"] = "old_format"
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", help="substring of the flight id")
    ap.add_argument("--stale", action="store_true", help="only flights with lines that are missing or in an old format")
    args = ap.parse_args()
    if not TRAITS.exists():
        sys.exit(f"{TRAITS} not reachable; is G: mounted?")

    flights, lines = [], []
    flight_ids = {p.name for p in TRAITS.iterdir() if p.is_dir() and p.name.startswith("NEON_")}
    for flight in sorted(flight_ids):
        if args.year and args.year not in flight:
            continue
        manifest = FLIGHTS_DIR / f"{flight}.json"
        stems = [x["name"][:-3] for x in json.loads(manifest.read_text())["lines"]] if manifest.exists() else \
            sorted(p.name[len("traits_"):-4] for p in (TRAITS / flight).glob("traits_*.tar"))
        rows = [line_row(flight, s) for s in stems]
        lines += rows
        n = len(rows)
        current = sum(r["status"] == "current" for r in rows)
        old = sum(r["status"] == "old_format" for r in rows)
        missing = sum(r["status"] == "missing" for r in rows)
        archived = (CORRECTION / flight / f"samples_{flight}.tar").exists()
        coeffs = (CORRECTION / flight / f"coeffs_{flight}.tar").exists()
        gb = round(sum(float(r["traits_tar_mb"] or 0) for r in rows) / 1e3, 2)
        flights.append({"flight_id": flight, "lines": n, "current": current, "old_format": old, "missing": missing,
                        "traits_gb": gb, "coeffs_archived": coeffs, "samples_archived": archived,
                        "last_written": max((r["written"] for r in rows if r["written"]), default="")})

    if args.stale:
        flights = [f for f in flights if f["old_format"] or f["missing"]]
    print(f"{'flight':32s} {'lines':>5s} {'cur':>4s} {'old':>4s} {'miss':>4s} {'GB':>6s} {'coef':>4s} {'smpl':>4s}  last written")
    for f in flights:
        print(f"{f['flight_id']:32s} {f['lines']:5d} {f['current']:4d} {f['old_format']:4d} {f['missing']:4d} "
              f"{f['traits_gb']:6.2f} {'y' if f['coeffs_archived'] else '-':>4s} {'y' if f['samples_archived'] else '-':>4s}  {f['last_written']}")
    tot = lambda k: sum(f[k] for f in flights)  # noqa: E731
    print(f"\n{len(flights)} flights, {tot('lines')} lines: {tot('current')} current, {tot('old_format')} old format, "
          f"{tot('missing')} missing; "
          f"{tot('traits_gb'):.1f} GB of trait maps; "
          f"{sum(f['samples_archived'] for f in flights)} flights with samples archived")

    LEDGER_DIR.mkdir(exist_ok=True)
    with open(LEDGER_DIR / "flights_ledger.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flights[0].keys()) if flights else ["flight_id"])
        w.writeheader(); w.writerows(flights)
    with open(LEDGER_DIR / "lines_ledger.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(lines[0].keys()) if lines else ["flight_id"])
        w.writeheader(); w.writerows(lines)
    print(f"ledger -> {LEDGER_DIR}")


if __name__ == "__main__":
    main()
