"""Stage A, one flightline: fit SCS+C topographic coefficients and extract FlexBRDF samples.

This is the per-line half of the HyTools "FlexBRDF without Ray" recipe
(hytools/examples/separated_flexbrdf.md), so that one CHTC job handles one ~5 GB
flightline and ships back only a few hundred MB. The flight-level BRDF fit happens in
2_fit_brdf.py once every line of the flight has run.

Outputs, under DATA_ROOT/20_neon_aop__hytools_correction/<flight_id>/:

    <line>_topo_coeffs.json     HyTools topo dict with one C per good band
    <line>_prebrdf_sample.h5    topo-corrected reflectance samples + BRDF kernel values
    <line>_sample_meta.json     counts, solar zenith, timings, processing version

Usage:

    uv run python src/20_neon_aop__hytools_correction/1_sample_line.py \
        --flight NEON_2017_D11_BLUE_20170506 --line NEON_D11_BLUE_DP1_20170506_160715_reflectance

The HDF5 is looked for under DATA_ROOT/10_.../h5/<flight_id>/ and downloaded from NEON if
absent (pass --h5 to point at a file elsewhere, e.g. a CHTC job's scratch directory).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import h5py
import hytools as ht
import numpy as np
from hytools.brdf import calc_brdf_coeffs_pre
from hytools.topo import calc_topo_coeffs_single

sys.path[:0] = [
    str(Path(__file__).resolve().parents[2]),
    str(Path(__file__).resolve().parents[1] / "00_common"),
]

import neon_api  # noqa: E402
from config.paths import CORRECTION_DIR, FLIGHTS_DIR, RAW_H5_DIR  # noqa: E402
from hytools_config import BAD_BANDS, DEFAULT_SAMPLE_PERC, PROCESSING_VERSION, sample_config  # noqa: E402
from project_env import neon_token  # noqa: E402
import hdf5_cache  # noqa: E402

hdf5_cache.install()  # before HyTools opens any file; see 00_common/hdf5_cache.py


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flight", required=True, help="flight id, e.g. NEON_2017_D11_BLUE_20170506")
    ap.add_argument("--line", required=True, help="flightline file name or stem")
    ap.add_argument("--h5", type=Path, help="path to the HDF5 if not under the local cache")
    ap.add_argument("--manifest", type=Path, help="flight manifest json (default: inventory layer)")
    ap.add_argument("--out-dir", type=Path, help="output directory (default: correction layer/<flight>)")
    ap.add_argument("--sample-perc", type=float, default=DEFAULT_SAMPLE_PERC)
    ap.add_argument("--delete-h5", action="store_true", help="remove the HDF5 afterwards (CHTC)")
    args = ap.parse_args()

    manifest_path = args.manifest or FLIGHTS_DIR / f"{args.flight}.json"
    manifest = neon_api.read_flight_manifest(manifest_path)
    line = neon_api.manifest_line(manifest, args.line)
    out_dir = args.out_dir or CORRECTION_DIR / args.flight
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    h5_path = args.h5 or neon_api.fetch_flightline(line, RAW_H5_DIR / args.flight, neon_token())
    t_download = time.time() - t0

    config = sample_config([h5_path], out_dir, args.sample_perc)
    (out_dir / f"{line.stem}_sample_config.json").write_text(json.dumps(config, indent=1))

    hdf5_cache.patch_neon_reader()   # some NEON files carry no wavelength Units attribute
    hy = ht.HyTools()
    hy.read_file(str(h5_path), "neon")
    hy.create_bad_bands(BAD_BANDS)
    hdf5_cache.keep_open(hy)
    print(f"{line.name}: {hy.lines} x {hy.columns} x {hy.bands}, "
          f"{int(hy.mask['no_data'].sum())} valid pixels")

    t1 = time.time()
    calc_topo_coeffs_single(hy, config["topo"])
    topo_path = out_dir / f"{line.stem}_topo_coeffs.json"
    topo_path.write_text(json.dumps(hy.topo))
    t_topo = time.time() - t1
    print(f"topo coefficients for {len(hy.topo['coeffs'])} bands in {t_topo:.0f}s")

    t2 = time.time()
    # A line where no pixel passes the BRDF mask (NDVI 0.1-1, slope, cos_i) has nothing to
    # contribute to the flight's fit. HyTools does not handle that: ndvi_stratify indexes
    # with the empty float array np.random.choice returns for an empty population, so it
    # raises IndexError (brdf/flex.py, seen on NEON_D10_CPER L024-1 2024-06-01, a
    # shortgrass line). Write no sample file; 2_fit_brdf.py pools the other lines and
    # records this one under lines_without_samples, and the topo coefficients above are
    # all stage C needs from this job.
    try:
        samples = calc_brdf_coeffs_pre(hy, config)
    except IndexError:
        n_brdf_px = int(hy.mask["calc_brdf"].sum()) if "calc_brdf" in hy.mask else -1
        if n_brdf_px != 0:
            raise
        samples, n, sample_path = None, 0, None
        print(f"no pixel passes the BRDF mask: no samples from this line ({n_brdf_px} eligible pixels)")
    if samples is not None:
        sample_path = out_dir / f"{line.stem}_prebrdf_sample.h5"
        _export_samples(hy, samples, sample_path)
        n = int(samples["kernel_samples"].shape[0])
        print(f"{n} BRDF samples ({sample_path.stat().st_size / 1e6:.0f} MB) in {time.time() - t2:.0f}s")
    t_brdf = time.time() - t2

    meta = {
        "flight_id": args.flight,
        "line": line.name,
        "processing_version": PROCESSING_VERSION,
        "sample_perc": args.sample_perc,
        "n_samples": n,
        "n_good_bands": len(samples["used_band"]) if samples else 0,
        "n_valid_pixels": int(hy.mask["no_data"].sum()),
        "n_brdf_mask_pixels": int(hy.mask["calc_brdf"].sum()) if "calc_brdf" in hy.mask else None,
        "line_mean_solar_zn_deg": float(np.degrees(samples["set_solar_zn"])) if samples else None,
        "seconds": {"download": round(t_download), "topo": round(t_topo), "brdf_samples": round(t_brdf)},
    }
    (out_dir / f"{line.stem}_sample_meta.json").write_text(json.dumps(meta, indent=1))

    hdf5_cache.release(hy)
    if args.delete_h5:
        Path(h5_path).unlink()
    print(f"done in {time.time() - t0:.0f}s -> {out_dir}")


def _export_samples(hy, d: dict, path: Path) -> None:
    """Same layout as hytools/scripts/no_ray/image_correct_get_sample_chtc.py, float32."""
    with h5py.File(path, "w") as f:
        k = f.create_dataset("kernels_samples", data=d["kernel_samples"].astype(np.float32))
        k.attrs["set_solar_zn"] = float(d["set_solar_zn"])
        k.attrs["kernels_names"] = '["Volume","Geometry"]'
        f.create_dataset("reflectance_samples", data=d["reflectance_samples"].astype(np.float32),
                         compression="gzip", compression_opts=1)
        f.create_dataset("wavelengths", data=np.array(d["used_band"]))
        f.create_dataset("image_wavelengths", data=np.array(hy.wavelengths))
        f.create_dataset("bad_bands", data=np.array(hy.bad_bands))


if __name__ == "__main__":
    main()
