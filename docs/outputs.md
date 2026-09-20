# Outputs

Each flightline produces one tarball plus a small JSON sidecar next to it with versions,
counts and timings, so you can check those without opening the tar. Everything is written
under your share folder, in layer folders with the same names as under `src/`.

## Trait maps: `30_neon_aop__trait_maps/<flight>/traits_<line>.tar`

| File | Contents |
| :--- | :--- |
| `<line>_traits.tif` | float32, tiled 256 x 96, zstd with predictor 3 after 10-bit mantissa truncation. Two bands per model, `<trait>_mean` and `<trait>_std` (over the model's permutation iterations), followed by `ndvi` from the corrected 850/660 nm bands. Nodata is -9999 |
| `<line>_quicklook.tif` | uint8 RGB from the corrected 660/550/480 nm bands, JPEG quality 85, overviews 2-32x, nodata 0. The stretch is the same for every line (reflectance 0-0.35 linear, then gamma 1.8), so lines and years can be compared visually and any remaining BRDF gradients stay visible. About 0.5 byte per pixel |
| `<line>_traits_qa.tif` | uint32, two bands, deflate. Band 1 `pixel_flags`: 1 valid, 2 NDVI in 0.1-1.0, 4 more than 30 px from the flightline edge, 8 ATCOR clear land, 16 shadow, 32 cloud/cirrus, 64 haze, 128 water; bits 8-15 hold the raw ATCOR class (255 if the layer is missing). Band 2 `model_in_range`: bit *i* is set when model *i* predicted inside its calibration range. The bit names are in the GeoTIFF tags and the sidecar |
| `<line>_traits.json` | band order, units, model and coefficient file names, processing version, timings |
| `<line>_trait_config.json` | the HyTools config that was used |

### Why the mantissa truncation

Lossless compression does little for noisy float32 because the low mantissa bits are
effectively random. Zeroing all but the top 10 mantissa bits before zstd with predictor 3
roughly halves the file size while keeping about three significant digits, which is well
below the prediction uncertainty. This is a lossy step and is recorded in the sidecar.

### A caveat on the QA bits

`pixel_flags` bit 2 comes from HyTools' `ndi` mask, which is computed on the raw
reflectance, while the `ndvi` band is computed after correction. About 0.1 % of pixels end
up with `|NDVI| > 1` after correction even though bit 2 is set, and their trait values are
not usable. Filter on the `ndvi` band as well as the bits.

## Corrections: `20_neon_aop__hytools_correction/<flight>/`

* `coeffs_<flight>.tar`: the per-line topographic coefficients and the flight's BRDF
  coefficients. Rerunning the trait maps needs only this file.
* `samples_<flight>.tar`: the stage A samples (2 % of the pixels). Refitting the BRDF with
  other kernels or bins needs only this, without downloading the imagery again.

## Units and normalisation

Corrected reflectance stays in NEON's 0-10 000 integer units; HyTools does not rescale.
`1_map_traits_line.py` divides by 10 000 before applying the models.

Each flight is BRDF-normalised to its own scene-mean solar zenith angle, so lines from
different days are not normalised to each other. Mosaicking, choosing which line to use in
overlaps, and exporting single-band per-trait files are outside the scope of this
repository.
