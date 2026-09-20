"""Stage B, one flight: pool the per-line BRDF samples and fit the FlexBRDF coefficients.

Reads every <line>_prebrdf_sample.h5 in the flight's correction folder, bins the pooled
pixels by NDVI (18 dynamic bins), fits f_iso/f_geo/f_vol per bin per band, and writes the
same coefficient dict once per flightline of the flight, because HyTools expects one
<line>_brdf_coeffs.json per image when it applies precomputed coefficients. Lines listed in
the manifest that have no sample file still get the flight's coefficients (the legacy
pipeline did the same by copying).

Outputs, under DATA_ROOT/20_neon_aop__hytools_correction/<flight_id>/:

    <line>_brdf_coeffs.json     one per manifest line
    brdf_fit_summary.json       lines used, samples, bins, scene solar zenith

Usage:

    uv run python src/20_neon_aop__hytools_correction/2_fit_brdf.py --flight NEON_2017_D11_BLUE_20170506
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import h5py
import numpy as np
from hytools.brdf import calc_flex_single_post

sys.path[:0] = [
    str(Path(__file__).resolve().parents[2]),
    str(Path(__file__).resolve().parents[1] / "00_common"),
]

import neon_api  # noqa: E402
from config.paths import CORRECTION_DIR, FLIGHTS_DIR  # noqa: E402
from hytools_config import DEFAULT_SAMPLE_PERC, PROCESSING_VERSION, brdf_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flight", required=True)
    ap.add_argument("--manifest", type=Path, help="flight manifest json (default: inventory layer)")
    ap.add_argument("--dir", type=Path, help="folder holding the sample files (default: correction layer/<flight>)")
    ap.add_argument("--sample-perc", type=float, default=DEFAULT_SAMPLE_PERC,
                    help="recorded in the coefficient file; must match stage A")
    args = ap.parse_args()

    work = args.dir or CORRECTION_DIR / args.flight
    manifest_path = args.manifest or FLIGHTS_DIR / f"{args.flight}.json"
    manifest = neon_api.read_flight_manifest(manifest_path)
    stems = [row["name"][:-3] for row in manifest["lines"]]

    sample_files = [work / f"{s}_prebrdf_sample.h5" for s in stems]
    present = [p for p in sample_files if p.exists()]
    missing = [p.name for p in sample_files if not p.exists()]
    if not present:
        sys.exit(f"no *_prebrdf_sample.h5 in {work}")
    print(f"{len(present)} of {len(stems)} lines have samples; missing: {missing or 'none'}")

    t0 = time.time()
    pooled = _load_samples(present)
    print(f"{pooled['kernels_samples'].shape[0]} pooled samples, "
          f"{pooled['reflectance_samples'].shape[1]} bands, loaded in {time.time() - t0:.0f}s")

    brdf = brdf_config(args.sample_perc)
    brdf["solar_zn_norm_radians"] = float(pooled["mean_solar_zn"])
    print(f"scene mean solar zenith: {np.degrees(brdf['solar_zn_norm_radians']):.2f} deg")

    t1 = time.time()
    calc_flex_single_post(pooled, brdf, 0)
    coeffs = pooled["brdf_dict"]
    print(f"fitted {len(coeffs['coeffs'])} bands x {len(coeffs['bins'])} NDVI bins in {time.time() - t1:.0f}s")

    for stem in stems:
        (work / f"{stem}_brdf_coeffs.json").write_text(json.dumps(coeffs))

    summary = {
        "flight_id": args.flight,
        "processing_version": PROCESSING_VERSION,
        "lines_in_manifest": len(stems),
        "lines_with_samples": [p.name.replace("_prebrdf_sample.h5", "") for p in present],
        "lines_without_samples": [m.replace("_prebrdf_sample.h5", "") for m in missing],
        "n_samples": int(pooled["kernels_samples"].shape[0]),
        "n_bands": int(pooled["reflectance_samples"].shape[1]),
        "ndvi_bins": coeffs["bins"],
        "scene_solar_zn_deg": float(np.degrees(brdf["solar_zn_norm_radians"])),
        "seconds": round(time.time() - t0),
    }
    (work / "brdf_fit_summary.json").write_text(json.dumps(summary, indent=1))
    print(f"wrote {len(stems)} coefficient files -> {work}")


def _load_samples(files: list[Path]) -> dict:
    """Pool the per-line sample files; mirrors load_sample_h5() in HyTools' no_ray scripts."""
    refl, kern, ndvi, solar = [], [], [], []
    bad_bands = None
    for i, path in enumerate(files):
        with h5py.File(path, "r") as f:
            waves = f["wavelengths"][()]
            r = f["reflectance_samples"][()]
            k = f["kernels_samples"][()]
            solar.append(float(f["kernels_samples"].attrs["set_solar_zn"]))
            if i == 0:
                bad_bands = f["bad_bands"][()]
        nir = r[:, np.argmin(np.abs(waves - 850))]
        red = r[:, np.argmin(np.abs(waves - 660))]
        ndvi.append((nir - red) / (nir + red))
        refl.append(r)
        kern.append(k)
    return {
        "kernels_samples": np.concatenate(kern),
        "reflectance_samples": np.concatenate(refl),
        "ndi_samples": np.concatenate(ndvi),
        "mean_solar_zn": float(np.mean(solar)),
        "bad_bands": bad_bands,
    }


if __name__ == "__main__":
    main()
