"""Stage C, one flightline: apply the precomputed corrections on the fly and map traits.

Reads the raw DP1 HDF5 again (downloading it if needed), corrects each chunk with the
flightline's topo coefficients and the flight's BRDF coefficients in memory, applies every
PLSR trait model, and writes the GeoTIFFs. Nothing corrected is written to disk; only
the trait maps leave the job.

Outputs, under DATA_ROOT/30_neon_aop__trait_maps/<flight_id>/:

    <line>_traits.tif    float32, two bands per model (<trait>_mean, <trait>_std), then ndvi;
                         nodata -9999
    <line>_traits_qa.tif uint32, two bands. Band 1 pixel_flags: bit 0 valid, 1 NDVI in [0.1, 1],
                         2 not within 30 px of the flightline edge, 3 ATCOR clear land,
                         4 ATCOR shadow, 5 ATCOR cloud/cirrus, 6 ATCOR haze, 7 ATCOR water,
                         bits 8-15 the raw ATCOR class (255 = layer missing). Band 2
                         model_in_range: bit i = model i prediction inside its calibration
                         range
    <line>_traits.json   band order, units, QA bit names, model files, coefficient files,
                         processing version

Usage:

    uv run python src/30_neon_aop__trait_maps/1_map_traits_line.py \
        --flight NEON_2017_D11_BLUE_20170506 --line NEON_D11_BLUE_DP1_20170506_160715_reflectance \
        --models /path/to/trait_models
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import hytools as ht
import numpy as np
import rasterio
from hytools.masks import mask_dict
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.windows import Window

sys.path[:0] = [
    str(Path(__file__).resolve().parents[2]),
    str(Path(__file__).resolve().parents[1] / "00_common"),
]

import atcor  # noqa: E402
import neon_api  # noqa: E402
from config.paths import CORRECTION_DIR, FLIGHTS_DIR, RAW_H5_DIR, TRAIT_MAP_DIR, TRAIT_MODEL_DIR  # noqa: E402
from hytools_config import BAD_BANDS, PROCESSING_VERSION, TRAIT_QA_MASKS, trait_config  # noqa: E402
from project_env import neon_token  # noqa: E402
import hdf5_cache  # noqa: E402

NODATA = -9999.0
# HyTools chunks are CHUNK_ROWS x CHUNK_COLS windows and the GeoTIFF tiles 96 x 256, so
# every window write is tile-aligned. Memory scales with the window, not the image:
# HyTools' BRDF application and cubic resampling hold ~10 float64 copies of a chunk. NEON
# stores a flightline with its long axis along rows (BLUE 2017: ~1 000 columns) or along
# columns (CLBJ 2019: 9 611 columns), so a full-width strip is not a safe unit.
CHUNK_ROWS = 96
CHUNK_COLS = 1024
BLOCK_ROWS = CHUNK_ROWS
BLOCK_COLS = 256
PRED_BLOCK_BYTES = 256 << 20  # cap on one (pixels x iterations) float32 prediction block
GDAL_CACHEMAX_MB = 512  # GDAL block cache for the two output GeoTIFFs; every tile completes within one window
# Output compression. Noisy float32 does not compress losslessly (deflate/LZW/zstd/LERC all
# land at or above the size of the valid pixels), so the low mantissa bits, which carry
# nothing but PLSR noise, are zeroed before writing: MANTISSA_BITS = 10 bounds the
# relative error at 2**-11 (about 5e-4, median 3e-4) and halves the file (a 1.2 GB line:
# 333 MB lossless -> 160 MB).
MANTISSA_BITS = 10
COMPRESSION = {"compress": "zstd", "zstd_level": 9, "predictor": 3}
#: Version of the *file format* written here (bands, QA layout, compression), as opposed
#: to PROCESSING_VERSION (correction parameters). Bump when a reader would have to change.
#:   2026-09-16.1  deflate lossless, uint16 single-band QA (overflowed past 13 models)
#:   2026-09-17.1  10-bit mantissa + zstd + predictor 3, band interleave
#:   2026-09-17.2  uint32 two-band QA with ATCOR flags and raw class
#:   2026-09-19.1  adds <line>_quicklook.tif (corrected RGB, uint8, JPEG, overviews)
TRAIT_MAP_FORMAT = "2026-09-19.1"
#: Quicklook: topo+BRDF corrected reflectance at the bands nearest these wavelengths (nm),
#: stretched linearly from 0 to QUICKLOOK_REFL_MAX reflectance and brightened with a gamma,
#: the same fixed stretch for every line so adjacent lines and years are comparable.
#: uint8 with 0 = nodata (valid pixels use 1-255), JPEG-compressed with internal overviews;
#: about 0.5 byte per pixel.
QUICKLOOK_BANDS_NM = (660, 550, 480)
QUICKLOOK_REFL_MAX = 0.35
QUICKLOOK_GAMMA = 1.8
QUICKLOOK_JPEG_QUALITY = 85
#: QA band 1 (pixel_flags) bit values; bits 8-15 hold the raw ATCOR class.
PIXEL_FLAGS = {"valid": 1, "ndvi_0.1_1.0": 2, "not_edge_30px": 4, "atcor_clear_land": 8,
               "atcor_shadow": 16, "atcor_cloud": 32, "atcor_haze": 64, "atcor_water": 128}
ATCOR_CLASS_SHIFT = 8


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flight", required=True)
    ap.add_argument("--line", required=True, help="flightline file name or stem")
    ap.add_argument("--h5", type=Path, help="path to the HDF5 if not under the local cache")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--coeff-dir", type=Path, help="folder with the topo/brdf json (default: correction layer/<flight>)")
    ap.add_argument("--models", type=Path, default=TRAIT_MODEL_DIR, help="folder of HyTools PLSR model json files")
    ap.add_argument("--out-dir", type=Path, help="default: trait-map layer/<flight>")
    ap.add_argument("--reflectance-scale", type=float, default=1.0,
                    help="multiply reflectance by this before the models. HyTools leaves NEON's "
                         "int16 values as stored (0-10000), which is the convention the lab's "
                         "NEON models were built on; pass 1e-4 for models fitted on 0-1 reflectance")
    ap.add_argument("--chunk-cols", type=int, default=CHUNK_COLS,
                    help="window width in pixels (multiple of 256); smaller values only for tests")
    ap.add_argument("--delete-h5", action="store_true")
    ap.add_argument("--no-pixel-major", action="store_true",
                    help="read windows from the HDF5 even when it is band-chunked (tests only; slow)")
    args = ap.parse_args()
    if args.chunk_cols % BLOCK_COLS:
        sys.exit(f"--chunk-cols must be a multiple of {BLOCK_COLS}")

    manifest = neon_api.read_flight_manifest(args.manifest or FLIGHTS_DIR / f"{args.flight}.json")
    line = neon_api.manifest_line(manifest, args.line)
    coeff_dir = args.coeff_dir or CORRECTION_DIR / args.flight
    out_dir = args.out_dir or TRAIT_MAP_DIR / args.flight
    out_dir.mkdir(parents=True, exist_ok=True)

    model_files = sorted(Path(args.models).glob("*.json"))
    if not model_files:
        sys.exit(f"no trait model json in {args.models}")
    models = [json.loads(p.read_text()) for p in model_files]

    t0 = time.time()
    h5_path = args.h5 or neon_api.fetch_flightline(line, RAW_H5_DIR / args.flight, neon_token())
    topo_json = coeff_dir / f"{line.stem}_topo_coeffs.json"
    brdf_json = coeff_dir / f"{line.stem}_brdf_coeffs.json"
    for p in (topo_json, brdf_json):
        if not p.exists():
            sys.exit(f"missing coefficient file {p}")
    config = trait_config(h5_path, topo_json, brdf_json, model_files, out_dir)
    (out_dir / f"{line.stem}_trait_config.json").write_text(json.dumps(config, indent=1))

    hdf5_cache.patch_neon_reader()   # some NEON files carry no wavelength Units attribute
    hy = ht.HyTools()
    hy.read_file(str(h5_path), "neon")
    hy.create_bad_bands(BAD_BANDS)
    # Chunk cache for two CHUNK_ROWS strips of the whole width (int16, all bands): a
    # 96-row strip overlaps two 93-row NEON chunk rows, and the windows of one strip are
    # read one after another. Clamped to 256 MB - 2 GB.
    strip = 2 * CHUNK_ROWS * hy.columns * hy.bands * 2
    hdf5_cache.install(nbytes=min(max(strip, 256 << 20), 2 << 30))
    hdf5_cache.keep_open(hy)
    # NEON files from 2022 on are chunked one band per chunk: window reads across the
    # spectrum then decompress the whole cube, so read them from a pixel-major copy.
    pixel_major = False
    if not args.no_pixel_major and hdf5_cache.band_major(h5_path):
        t_bip = time.time()
        bip = hdf5_cache.pixel_major_copy(h5_path)
        hdf5_cache.use_pixel_major(hy, bip)
        pixel_major = True
        print(f"band-chunked HDF5: pixel-major copy {bip.name} ({bip.stat().st_size / 1e9:.1f} GB) "
              f"ready in {time.time() - t_bip:.0f}s")
    hy.corrections = ["topo", "brdf"]
    hy.load_coeffs(str(topo_json), "topo")
    hy.load_coeffs(str(brdf_json), "brdf")
    hy.resampler["type"] = config["resampling"]["type"]
    for name, margs in TRAIT_QA_MASKS:
        hy.gen_mask(mask_dict[name], name, margs)
    if len(models) > 32:
        sys.exit(f"{len(models)} models: the model_in_range QA band holds at most 32")
    atcor_map = atcor.read_class_map(h5_path, hy.base_key)
    print(f"{line.name}: {hy.lines} x {hy.columns}, {len(models)} models, windows {CHUNK_ROWS} x {args.chunk_cols}, "
          f"ATCOR map {'present' if atcor_map is not None else 'missing'}")

    tif = out_dir / f"{line.stem}_traits.tif"
    qa_tif = out_dir / f"{line.stem}_traits_qa.tif"
    ql_tif = out_dir / f"{line.stem}_quicklook.tif"
    profile = _profile(hy)
    ql_profile = {**profile, "interleave": "pixel"}
    # Bands: two per model, then one NDVI band from the corrected reflectance.
    with rasterio.Env(GDAL_CACHEMAX=GDAL_CACHEMAX_MB), \
         rasterio.open(tif, "w", **profile, count=2 * len(models) + 1, dtype="float32", nodata=NODATA,
                       **COMPRESSION) as dst, \
         rasterio.open(qa_tif, "w", **profile, count=2, dtype="uint32", nodata=0,
                       compress="deflate", predictor=2) as qa, \
         rasterio.open(ql_tif, "w", **ql_profile, count=3, dtype="uint8", nodata=0,
                       compress="jpeg", jpeg_quality=QUICKLOOK_JPEG_QUALITY, photometric="ycbcr") as ql:
        names = []
        for i, m in enumerate(models):
            names += [f"{m['name']}_mean", f"{m['name']}_std"]
            dst.set_band_description(2 * i + 1, names[-2])
            dst.set_band_description(2 * i + 2, names[-1])
        names.append("ndvi")
        dst.set_band_description(len(names), "ndvi")
        dst.update_tags(processing_version=PROCESSING_VERSION, format_version=TRAIT_MAP_FORMAT, flight_id=args.flight,
                        mantissa_bits=MANTISSA_BITS, max_relative_error=f"{2.0 ** -(MANTISSA_BITS + 1):.1e}")
        qa.set_band_description(1, "pixel_flags")
        qa.set_band_description(2, "model_in_range")
        qa.update_tags(processing_version=PROCESSING_VERSION, flight_id=args.flight,
                       pixel_flags=json.dumps(PIXEL_FLAGS), atcor_class_bits=f"{ATCOR_CLASS_SHIFT}-{ATCOR_CLASS_SHIFT + 7}",
                       model_in_range=json.dumps({str(1 << i): m["name"] for i, m in enumerate(models)}))
        for b, nm in enumerate(QUICKLOOK_BANDS_NM, 1):
            ql.set_band_description(b, f"{nm}nm")
        ql.update_tags(processing_version=PROCESSING_VERSION, format_version=TRAIT_MAP_FORMAT, flight_id=args.flight,
                       bands_nm=json.dumps(QUICKLOOK_BANDS_NM), reflectance_max=QUICKLOOK_REFL_MAX,
                       gamma=QUICKLOOK_GAMMA, corrections="topo+brdf")

        # One pass over the image per distinct model wavelength grid.
        groups: dict[tuple, list[int]] = {}
        for i, m in enumerate(models):
            groups.setdefault(tuple(m["wavelengths"]), []).append(i)
        for gi, (waves, idx) in enumerate(groups.items()):
            _map_group(hy, models, idx, np.array(waves), dst, qa, first_pass=(gi == 0),
                       scale=args.reflectance_scale, chunk_cols=args.chunk_cols, atcor_map=atcor_map, ql=ql)
    # Internal overviews so a viewer opens the whole line at once; JPEG like the base.
    with rasterio.Env(COMPRESS_OVERVIEW="JPEG", JPEG_QUALITY_OVERVIEW=QUICKLOOK_JPEG_QUALITY,
                      PHOTOMETRIC_OVERVIEW="YCBCR"), rasterio.open(ql_tif, "r+") as ql:
        ql.build_overviews([2, 4, 8, 16, 32], Resampling.average)

    sidecar = {
        "flight_id": args.flight,
        "line": line.name,
        "processing_version": PROCESSING_VERSION,
        "format_version": TRAIT_MAP_FORMAT,
        "bands": names,
        "units": {m["name"]: m.get("units", "") for m in models},
        "qa": {
            "band_1_pixel_flags": {**{str(v): k for k, v in PIXEL_FLAGS.items()},
                                   f"bits_{ATCOR_CLASS_SHIFT}_{ATCOR_CLASS_SHIFT + 7}": "atcor_class (255 = layer missing)"},
            "band_2_model_in_range": {str(1 << i): f"{m['name']}_in_range" for i, m in enumerate(models)},
            "atcor_classes": {str(k): v for k, v in atcor.CLASS_NAMES.items()},
            "atcor_layer_present": atcor_map is not None,
        },
        "trait_models": [p.name for p in model_files],
        "reflectance_scale": args.reflectance_scale,
        "compression": {**COMPRESSION, "mantissa_bits": MANTISSA_BITS,
                        "max_relative_error": 2.0 ** -(MANTISSA_BITS + 1)},
        "topo_coeffs": topo_json.name,
        "brdf_coeffs": brdf_json.name,
        "pixel_major_copy": pixel_major,
        "quicklook": {"file": ql_tif.name, "bands_nm": list(QUICKLOOK_BANDS_NM), "corrections": "topo+brdf",
                      "reflectance_max": QUICKLOOK_REFL_MAX, "gamma": QUICKLOOK_GAMMA, "dtype": "uint8",
                      "nodata": 0, "compression": f"jpeg q{QUICKLOOK_JPEG_QUALITY}", "overviews": [2, 4, 8, 16, 32]},
        "seconds": round(time.time() - t0),
    }
    (out_dir / f"{line.stem}_traits.json").write_text(json.dumps(sidecar, indent=1))
    hdf5_cache.release(hy)
    if args.delete_h5:
        Path(h5_path).unlink()
        hdf5_cache.remove_pixel_major(h5_path)
    print(f"done in {time.time() - t0:.0f}s -> {tif} ({tif.stat().st_size / 1e6:.0f} MB)")


def _profile(hy) -> dict:
    try:
        crs = CRS.from_wkt(hy.projection)
    except Exception:  # NEON files also carry an EPSG code in Coordinate_System
        import h5py

        with h5py.File(hy.file_name, "r") as f:
            epsg = int(f[hy.base_key]["Reflectance"]["Metadata"]["Coordinate_System"]["EPSG Code"][()])
        crs = CRS.from_epsg(epsg)
    return {
        "driver": "GTiff",
        "height": hy.lines,
        "width": hy.columns,
        "crs": crs,
        "transform": Affine.from_gdal(*hy.transform),
        "tiled": True,
        "blockxsize": BLOCK_COLS,
        "blockysize": BLOCK_ROWS,
        # Band interleave: with GDAL's default pixel interleave a tile holds all bands, so
        # writing the bands of a window one after another re-encodes and re-appends the
        # same tile up to once per band (a 4.3 GB file for 160 MB of data).
        # One tile per band is also what a per-trait reader wants.
        "interleave": "band",
        "BIGTIFF": "IF_SAFER",
    }


def _truncate(x: np.ndarray, keep_bits: int = MANTISSA_BITS) -> np.ndarray:
    """Zero the low (23 - keep_bits) mantissa bits of a float32 array, in place.

    Relative error is at most 2**-(keep_bits + 1); the value stays a valid float32, so
    every reader sees an ordinary GeoTIFF. Nodata is written after this, because -9999
    itself needs 13 mantissa bits.
    """
    u = x.view(np.uint32)
    u &= np.uint32(0xFFFFFFFF) << np.uint32(23 - keep_bits)
    return x


def _map_group(hy, models, idx, model_waves, dst, qa, first_pass: bool, scale: float = 1.0,
               chunk_cols: int = CHUNK_COLS, atcor_map=None, ql=None) -> None:
    """Apply the models in idx (all sharing model_waves) over the image, writing windows."""
    resample = not all(w in hy.wavelengths for w in model_waves)
    if resample:
        hy.resampler["out_waves"] = model_waves
        hy.resampler["out_fwhm"] = models[idx[0]]["fwhm"]
        wave_mask = None
    else:
        wave_mask = [int(np.argwhere(w == hy.wavelengths)[0][0]) for w in model_waves]

    # Models sharing a transform chain share the normalised chunk and one matrix product:
    # all their permutation coefficients are stacked into a single (iterations, bands)
    # matrix, so N models cost one BLAS call per chunk instead of N einsums.
    by_transform: dict[tuple, list[int]] = {}
    for i in idx:
        by_transform.setdefault(tuple(models[i]["model"]["transform"]), []).append(i)
    groups = []
    for transforms, members in by_transform.items():
        coeffs = np.concatenate([np.asarray(models[i]["model"]["coefficients"], dtype=np.float32) for i in members])
        intercepts = np.concatenate([np.asarray(models[i]["model"]["intercepts"], dtype=np.float32) for i in members])
        sizes = [len(models[i]["model"]["intercepts"]) for i in members]
        offsets = np.cumsum([0] + sizes)
        bounds = [(models[i]["model_diagnostics"]["min"], models[i]["model_diagnostics"]["max"]) for i in members]
        groups.append((transforms, members, coeffs, intercepts, offsets, bounds))

    it = hy.iterate(by="chunk", chunk_size=(CHUNK_ROWS, chunk_cols), corrections=hy.corrections, resample=resample)
    chunk_waves = model_waves if resample else hy.wavelengths
    i_nir, i_red = (int(np.argmin(np.abs(chunk_waves - w))) for w in (850, 660))
    i_rgb = [int(np.argmin(np.abs(chunk_waves - w))) for w in QUICKLOOK_BANDS_NM]
    refl_per_unit = 1e-4 / scale  # chunk values are NEON 0-10000 times `scale`
    ndvi_band = 2 * len(models) + 1

    while not it.complete:
        chunk = it.read_next()
        y0, x0 = it.current_line, it.current_column
        h, w = chunk.shape[:2]
        win = Window(x0, y0, w, h)
        valid = hy.mask["no_data"][y0:y0 + h, x0:x0 + w]

        if first_pass:
            # NDVI from the topo+BRDF corrected reflectance, same bands the masks use.
            nir = chunk[:, :, i_nir].astype(np.float32)
            red = chunk[:, :, i_red].astype(np.float32)
            with np.errstate(divide="ignore", invalid="ignore"):
                ndvi = (nir - red) / (nir + red)
            bad = ~valid | ~np.isfinite(ndvi)
            _truncate(ndvi)[bad] = NODATA
            dst.write(ndvi, indexes=ndvi_band, window=win)

            if ql is not None:
                rgb = chunk[:, :, i_rgb].astype(np.float32) * np.float32(refl_per_unit / QUICKLOOK_REFL_MAX)
                np.clip(rgb, 0, 1, out=rgb)
                rgb = np.rint(1 + 254 * rgb ** np.float32(1 / QUICKLOOK_GAMMA)).astype(np.uint8)
                rgb[~valid] = 0
                ql.write(np.moveaxis(rgb, 2, 0), window=win)

            flags = valid.astype(np.uint32) * PIXEL_FLAGS["valid"]
            flags |= hy.mask["ndi"][y0:y0 + h, x0:x0 + w].astype(np.uint32) * PIXEL_FLAGS["ndvi_0.1_1.0"]
            flags |= hy.mask["neon_edge"][y0:y0 + h, x0:x0 + w].astype(np.uint32) * PIXEL_FLAGS["not_edge_30px"]
            if atcor_map is not None:
                cls = atcor_map[y0:y0 + h, x0:x0 + w]
                for name, members in (("atcor_clear_land", atcor.CLEAR_LAND), ("atcor_shadow", atcor.SHADOW),
                                      ("atcor_cloud", atcor.CLOUD), ("atcor_haze", atcor.HAZE),
                                      ("atcor_water", atcor.WATER)):
                    flags |= atcor.isin(cls, members).astype(np.uint32) * PIXEL_FLAGS[name]
            else:
                cls = np.full((h, w), atcor.MISSING, np.uint8)
            flags |= cls.astype(np.uint32) << ATCOR_CLASS_SHIFT
            flags[~valid] = 0
            qa.write(flags, indexes=1, window=win)
            bits = np.zeros((h, w), np.uint32)
        else:
            bits = qa.read(2, window=win)
        if wave_mask is not None:
            chunk = chunk[:, :, wave_mask]

        base = chunk.astype(np.float32)
        if scale != 1.0:
            base *= np.float32(scale)
        for transforms, members, coeffs, intercepts, offsets, bounds in groups:
            x = base
            for t in transforms:
                if t == "vector":
                    x = x / np.linalg.norm(x, axis=2, keepdims=True)
                elif t == "absorb":
                    x = np.log(1 / x)
                elif t == "mean":
                    x = x / x.mean(axis=2, keepdims=True)
            # (pixels, bands) @ (bands, all iterations of all models in the group), done
            # in pixel slabs so the prediction block stays under PRED_BLOCK_BYTES: with
            # 30 models x 200 iterations on a 96 x 1024 window would need 2.3 GB.
            x2d = x.reshape(-1, x.shape[2])
            n_px, n_iter = x2d.shape[0], coeffs.shape[0]
            slab = max(1, PRED_BLOCK_BYTES // (4 * n_iter))
            means = np.empty((len(members), n_px), np.float32)
            stds = np.empty((len(members), n_px), np.float32)
            for s0 in range(0, n_px, slab):
                pred = x2d[s0:s0 + slab] @ coeffs.T + intercepts
                for k in range(len(members)):
                    p = pred[:, offsets[k]:offsets[k + 1]]
                    means[k, s0:s0 + slab] = p.mean(axis=1)
                    stds[k, s0:s0 + slab] = p.std(axis=1, ddof=1)
            for k, i in enumerate(members):
                mean = means[k].reshape(h, w)
                std = stds[k].reshape(h, w)
                # log(1/R) or a vector norm on a pixel with a zero or negative band (deep
                # shadow, water) gives inf/nan; those pixels get nodata, not a flag.
                ok = valid & np.isfinite(mean) & np.isfinite(std)
                lo, hi = bounds[k]
                in_range = (mean > lo) & (mean < hi) & ok
                _truncate(mean)[~ok] = NODATA
                _truncate(std)[~ok] = NODATA
                dst.write(mean, indexes=2 * i + 1, window=win)
                dst.write(std, indexes=2 * i + 2, window=win)
                bits |= in_range.astype(np.uint32) << np.uint32(i)
        qa.write(bits, indexes=2, window=win)


if __name__ == "__main__":
    main()
