"""The frozen HyTools settings, as plain dicts.

Every flight is corrected with exactly these parameters so that no model
downstream can learn a processing generation instead of an ecological signal. Change a
value here and every flight processed afterwards differs from every flight processed
before, so treat this file as a versioned decision and bump PROCESSING_VERSION.

The choices are the configuration Zhiwei Ye (HyTools / FlexBRDF author) uses for NEON
flightlines, from his example image_correct config for a 2019 SOAP flight of ten
lines: SCS+C topographic correction fitted with NNLS on pixels with NDVI 0.1-1.0,
slope >= 5 deg and cos(i) >= 0.12; FlexBRDF with the li_sparse_r geometric and ross_thick
volume kernels, b/r 2.5, h/b 2, 18 dynamic NDVI bins between the 10th and 95th
percentile, normalised to the scene-mean solar zenith angle, fitted on pixels with NDVI
0.1-1.0, finite kernels, view zenith >= 2 deg and more than 30 px from the flightline
edge, applied to pixels with NDVI 0.1-1.0. No cloud/shadow screen. The one deliberate
departure is sample_perc: his example uses 0.1 in one shared-memory process; on CHTC
each flightline extracts its samples in its own job and ships them to a combine job that
must hold a whole flight in memory, so the default here is 0.02 (about 150 MB of
float32 samples per 10 GB of HDF5 instead of 750 MB). It only changes the sampling
noise of the fit.
"""

from __future__ import annotations

import math
from pathlib import Path

PROCESSING_VERSION = "2026-09-16.1"

#: Wavelength ranges (nm) excluded from every fit and from the exported products.
BAD_BANDS = [[300, 400], [1337, 1430], [1800, 1960], [2450, 2600]]

#: Default fraction of eligible pixels each flightline contributes to the BRDF fit.
DEFAULT_SAMPLE_PERC = 0.02

_NDVI = ["ndi", {"band_1": 850, "band_2": 660, "min": 0.1, "max": 1.0}]
_SLOPE = ["ancillary", {"name": "slope", "min": math.radians(5), "max": "+inf"}]
_COSINE_I = ["ancillary", {"name": "cosine_i", "min": 0.12, "max": "+inf"}]


def topo_config() -> dict:
    """SCS+C topographic correction, one C per band per flightline."""
    return {
        "type": "scs+c",
        "c_fit_type": "nnls",
        "calc_mask": [_NDVI, _SLOPE, _COSINE_I],
        "apply_mask": [_NDVI, _SLOPE, _COSINE_I],
    }


def brdf_config(sample_perc: float = DEFAULT_SAMPLE_PERC) -> dict:
    """FlexBRDF, grouped over all flightlines of one flight, normalised to nadir view at
    the scene-mean solar zenith angle."""
    return {
        "type": "flex",
        "grouped": True,
        "geometric": "li_sparse_r",
        "volume": "ross_thick",
        "b/r": 2.5,
        "h/b": 2,
        "sample_perc": sample_perc,
        "interp_kind": "linear",
        "solar_zn_type": "scene",
        "bin_type": "dynamic",
        "num_bins": 18,
        "ndvi_bin_min": 0.05,
        "ndvi_bin_max": 1.0,
        "ndvi_perc_min": 10,
        "ndvi_perc_max": 95,
        "calc_mask": [
            _NDVI,
            ["kernel_finite", {}],
            ["ancillary", {"name": "sensor_zn", "min": math.radians(2), "max": "inf"}],
            ["neon_edge", {"radius": 30}],
        ],
        "apply_mask": [_NDVI],
    }


def sample_config(h5_files: list[Path], output_dir: Path, sample_perc: float) -> dict:
    """image_correct-style config for the per-line stage: fit topo, extract BRDF samples."""
    output_dir = str(output_dir).rstrip("/\\") + "/"
    return {
        "processing_version": PROCESSING_VERSION,
        "file_type": "neon",
        "input_files": [str(p) for p in h5_files],
        "bad_bands": BAD_BANDS,
        "corrections": ["topo", "brdf"],
        "topo": topo_config(),
        "brdf": brdf_config(sample_perc),
        "export": {
            "coeffs": True,
            "image": False,
            "masks": False,
            "subset_waves": [],
            "output_dir": output_dir,
            "suffix": "topo_brdf",
        },
        "resample": False,
        "num_cpus": 1,
    }


#: Mask layers appended to every trait map as QA bits, in this order.
TRAIT_QA_MASKS = [
    ["ndi", {"band_1": 850, "band_2": 660, "min": 0.1, "max": 1.0}],
    ["neon_edge", {"radius": 30}],
]


def trait_config(
    h5_file: Path,
    topo_json: Path,
    brdf_json: Path,
    trait_models: list[Path],
    output_dir: Path,
) -> dict:
    """trait_estimate-style config: apply precomputed coefficients on the fly, then models."""
    h5 = str(h5_file)
    return {
        "processing_version": PROCESSING_VERSION,
        "file_type": "neon",
        "input_files": [h5],
        "bad_bands": BAD_BANDS,
        "corrections": ["topo", "brdf"],
        "topo": {h5: str(topo_json)},
        "brdf": {h5: str(brdf_json)},
        "resampling": {"type": "cubic"},
        "masks": TRAIT_QA_MASKS,
        "trait_models": [str(p) for p in trait_models],
        "output_dir": str(output_dir).rstrip("/\\") + "/",
        "num_cpus": 1,
    }
