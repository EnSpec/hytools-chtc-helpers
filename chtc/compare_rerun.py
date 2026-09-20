"""Compare rerun trait maps on the lab server with the ones moved to z_archive_*/.

After a format change the old tarballs of a line are moved to
ENSPEC_PROJECT_DIR/z_archive_<date>_<reason>/30_neon_aop__trait_maps/<flight>/ and the
line is rerun (make_batch_dag.py --lines-csv). This script checks, line by line, that
the rerun changed only what the format change was meant to change:

    - the 59 trait bands (29 x mean, 29 x std, ndvi) agree to float noise: at most one
      10-bit-mantissa rounding step (relative 2^-10) on a negligible fraction of pixels.
      Two runs of the same line are not bit-identical: summation order in the
      correction moves a few reflectances by one float ulp and the lossy rounding then
      flips one quantum at a handful of pixels (JERC 0907 line 181822: 3 735 of
      7.4 M x 58 values, at most one quantum each). Values near zero are compared on the
      band's scale, not relatively;
    - the nodata masks are identical;
    - the QA low bits (valid, ndvi, not_edge) are identical, and the old in-range bits
      (bits 3.. of the uint16 QA, at most 13 models) equal the low bits of the new
      model_in_range band.

Runs on Windows against the mounted share, in the uv environment (rasterio). Tarballs are opened
with Python's tarfile (no --force-local quirk), the two GeoTIFFs are extracted to a
temporary folder and deleted afterwards.

    uv run python chtc/compare_rerun.py                     # every line present on both sides
    uv run python chtc/compare_rerun.py --flight CLBJ       # substring filter
    uv run python chtc/compare_rerun.py --limit 5 --redo    # first 5, recompare already listed lines

Appends one row per line to ENSPEC_PROJECT_DIR/ledger/compare_rerun.csv (lines already
in the CSV are skipped unless --redo) and prints a summary. A "differs" verdict means a
code change, not float noise, and needs a look before the archived folder is dropped.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import numpy as np
import rasterio

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config.paths import ENSPEC_PROJECT_DIR  # noqa: E402

TRAITS = ENSPEC_PROJECT_DIR / "30_neon_aop__trait_maps"
OUT_CSV = ENSPEC_PROJECT_DIR / "ledger" / "compare_rerun.csv"
FIELDS = ["archive", "flight_id", "line", "old_tif_mb", "new_tif_mb", "n_valid", "mask_equal", "max_rel_diff",
          "frac_over_tol", "worst_band", "ndvi_max_diff", "qa_flags_equal", "in_range_frac_diff", "verdict"]
REL_TOL = 2.0e-3    # two 10-bit rounding quanta (2^-10 each) relative to the value or the band's scale
FRAC_TOL = 1e-4     # fraction of valid pixels (per band) allowed above REL_TOL
NDVI_TOL = 1e-3     # ndvi is stored with the same rounding: one quantum at 0.5 is 4.9e-4; pixels with
                    # nir + red ~ 0 give |ndvi| >> 1 and flip wildly, so the fraction above NDVI_TOL counts


def extract(tar_path: Path, suffix: str, dest: Path) -> Path:
    with tarfile.open(tar_path) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith(suffix))
        tf.extract(member, dest, filter="data")
        return dest / member.name


def compare_line(old_tar: Path, new_tar: Path, tmp: Path) -> dict:
    row = {"old_tif_mb": round(old_tar.stat().st_size / 1e6), "new_tif_mb": round(new_tar.stat().st_size / 1e6)}
    old_dir, new_dir = tmp / "old", tmp / "new"
    old_tif = extract(old_tar, "_traits.tif", old_dir)
    new_tif = extract(new_tar, "_traits.tif", new_dir)
    old_qa = extract(old_tar, "_traits_qa.tif", old_dir)
    new_qa = extract(new_tar, "_traits_qa.tif", new_dir)

    max_rel, worst, mask_equal, ndvi_max, ndvi_frac, n_valid, frac_over = 0.0, "", True, 0.0, 0.0, 0, 0.0
    with rasterio.open(old_tif) as a, rasterio.open(new_tif) as b:
        if a.count != b.count or a.shape != b.shape:
            return {**row, "verdict": f"shape {a.count}x{a.shape} vs {b.count}x{b.shape}"}
        for i in range(1, a.count + 1):
            x = a.read(i)
            y = b.read(i)
            mx, my = x == a.nodata, y == b.nodata
            if not np.array_equal(mx, my):
                mask_equal = False
            ok = ~mx & ~my
            if i == 1:
                n_valid = int(ok.sum())
            if a.descriptions[i - 1] == "ndvi":
                if ok.any():
                    d = np.abs(x[ok].astype(np.float64) - y[ok])
                    ndvi_max = float(d.max())
                    # nir + red ~ 0 makes ndvi huge and sensitive to one ulp: judge by the fraction
                    ndvi_frac = float((d > NDVI_TOL).mean())
                continue
            if ok.any():
                xv, yv = x[ok].astype(np.float64), y[ok].astype(np.float64)
                scale = 1e-3 * float(np.percentile(np.abs(xv), 99)) + 1e-12  # floor for near-zero values
                rel = np.abs(xv - yv) / np.maximum(np.maximum(np.abs(xv), np.abs(yv)), scale)
                r = float(rel.max())
                frac_over = max(frac_over, float((rel > REL_TOL).mean()))
                if r > max_rel:
                    max_rel, worst = r, a.descriptions[i - 1] or f"band{i}"
    with rasterio.open(old_qa) as a, rasterio.open(new_qa) as b:
        oq = a.read(1).astype(np.uint32)
        nf = b.read(1)
        nr = b.read(2) if b.count >= 2 else np.zeros_like(nf)
        qa_flags_equal = bool(np.array_equal(oq & 7, nf & 7))
        n_old_models = max(0, a.dtypes[0] == "uint16" and 13 or 0)
        old_in = (oq >> 3) & ((1 << n_old_models) - 1)
        in_range_frac = float((old_in != (nr & ((1 << n_old_models) - 1))).mean())
    shutil.rmtree(old_dir, ignore_errors=True)
    shutil.rmtree(new_dir, ignore_errors=True)
    verdict = "ok" if (mask_equal and qa_flags_equal and ndvi_frac <= FRAC_TOL and frac_over <= FRAC_TOL
                       and in_range_frac <= FRAC_TOL) else "differs"
    return {**row, "n_valid": n_valid, "mask_equal": mask_equal, "max_rel_diff": f"{max_rel:.2e}",
            "frac_over_tol": f"{frac_over:.1e}", "worst_band": worst, "ndvi_max_diff": f"{ndvi_max:.1e}",
            "qa_flags_equal": qa_flags_equal, "in_range_frac_diff": f"{in_range_frac:.1e}", "verdict": verdict}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flight", help="substring of the flight id")
    ap.add_argument("--line", help="substring of the line name")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many lines (0 = all)")
    ap.add_argument("--redo", action="store_true", help="recompare lines already in the CSV")
    ap.add_argument("--tmp", type=Path, default=None, help="scratch folder for the extracted GeoTIFFs (a fast local disk)")
    args = ap.parse_args()

    done: set[tuple[str, str]] = set()
    if OUT_CSV.exists() and not args.redo:
        with open(OUT_CSV, newline="") as fh:
            done = {(r["flight_id"], r["line"]) for r in csv.DictReader(fh)}

    pairs = []
    for arch in sorted(ENSPEC_PROJECT_DIR.glob("z_archive_*")):
        for flight_dir in sorted((arch / "30_neon_aop__trait_maps").glob("NEON_*")):
            if args.flight and args.flight not in flight_dir.name:
                continue
            for old_tar in sorted(flight_dir.glob("traits_*.tar")):
                stem = old_tar.name[len("traits_"):-len(".tar")]
                new_tar = TRAITS / flight_dir.name / old_tar.name
                # the loose sidecar is the last file the job uploads: without it the tar may be partial
                if not new_tar.exists() or not (TRAITS / flight_dir.name / f"{stem}_traits.json").exists():
                    continue
                if (flight_dir.name, stem) in done or (args.line and args.line not in stem):
                    continue
                pairs.append((arch.name, flight_dir.name, stem, old_tar, new_tar))
    if args.limit:
        pairs = pairs[:args.limit]
    print(f"{len(pairs)} lines to compare ({len(done)} already in {OUT_CSV.name})")

    OUT_CSV.parent.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="compare_rerun_", dir=args.tmp))
    n_ok = 0
    try:
        with open(OUT_CSV, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if fh.tell() == 0:
                w.writeheader()
            for arch, flight, stem, old_tar, new_tar in pairs:
                row = {"archive": arch, "flight_id": flight, "line": stem}
                try:
                    row.update(compare_line(old_tar, new_tar, tmp))
                except Exception as exc:  # a partial tar, a missing member
                    row["verdict"] = f"error: {exc}"[:120]
                w.writerow({k: row.get(k, "") for k in FIELDS})
                fh.flush()
                n_ok += row.get("verdict") == "ok"
                print(f"{flight} {stem[-23:-13]}  {row.get('verdict')}  rel {row.get('max_rel_diff', '')} "
                      f"frac {row.get('frac_over_tol', '')} ndvi {row.get('ndvi_max_diff', '')} "
                      f"mask {row.get('mask_equal', '')} qa {row.get('qa_flags_equal', '')}/{row.get('in_range_frac_diff', '')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{n_ok} ok of {len(pairs)}; rows in {OUT_CSV}")


if __name__ == "__main__":
    main()
