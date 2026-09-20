"""Every filesystem path and remote location used by these helpers.

All paths live here; the scripts, submit files and one-off tools do not contain literal
paths. Anything that differs between people (CHTC NetID, data location, which share folder
to write to) is read from ``config/local_paths.py``, which is gitignored. Copy
``config/local_paths.example.py`` to ``config/local_paths.py`` and edit it once.

The scripts import the names defined here, so keep the names stable when changing values.
"""

from __future__ import annotations

from pathlib import Path

try:
    from config import local_paths as _local
except ImportError as exc:  # pragma: no cover - first-run guidance
    raise ImportError(
        "config/local_paths.py is missing. Copy config/local_paths.example.py to "
        "config/local_paths.py and edit the roots there."
    ) from exc


def _need(name: str):
    value = getattr(_local, name, None)
    if value in (None, ""):
        raise ValueError(f"config/local_paths.py does not set {name}; see local_paths.example.py")
    return value


# --------------------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------------------

#: The repository itself. Code and text only — never data.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Everything that is not code: the flightline inventory, cached HDF5 for local tests,
#: the trait models, and anything you choose to keep locally.
DATA_ROOT = Path(_need("DATA_ROOT"))

#: Throwaway work. Gitignored, never imported from, safe to delete at any time.
SCRATCH_DIR = PROJECT_ROOT / "scratch"


# --------------------------------------------------------------------------------------
# Layers
# --------------------------------------------------------------------------------------
# The data root mirrors the numbering under src/, so src/30_neon_aop__trait_maps/ reads
# and writes DATA_ROOT/30_neon_aop__trait_maps/. The same folder names appear on the
# output share, which is what ledger.py reads.

#: 00 — inputs this code does not produce: the PLSR trait models in HyTools JSON format.
REFERENCE_DIR = DATA_ROOT / "00_reference"
TRAIT_MODEL_DIR = Path(getattr(_local, "TRAIT_MODEL_DIR", REFERENCE_DIR / "trait_models"))

#: 10 — the NEON DP1.30006.001 flightline inventory from the NEON API, grouped into
#: flights (one site, one day), plus a local HDF5 cache used only for tests on this
#: machine. CHTC jobs never see that cache; they download in-job and delete.
INVENTORY_DIR = DATA_ROOT / "10_neon_aop__flightline_inventory"
FLIGHTS_DIR = INVENTORY_DIR / "flights"
RAW_H5_DIR = INVENTORY_DIR / "h5"

#: 20 — HyTools topographic + FlexBRDF correction: per-line topo coefficients, per-line
#: BRDF samples, per-flight BRDF coefficients.
CORRECTION_DIR = DATA_ROOT / "20_neon_aop__hytools_correction"

#: 30 — trait maps, one GeoTIFF per flightline.
TRAIT_MAP_DIR = DATA_ROOT / "30_neon_aop__trait_maps"

_LAYERS: dict[str, Path] = {
    "00_reference": REFERENCE_DIR,
    "10_neon_aop__flightline_inventory": INVENTORY_DIR,
    "20_neon_aop__hytools_correction": CORRECTION_DIR,
    "30_neon_aop__trait_maps": TRAIT_MAP_DIR,
}


def layer(name: str) -> Path:
    """Return the directory for a registered data layer, creating it if needed."""
    if name not in _LAYERS:
        raise KeyError(f"Unknown layer {name!r}; known layers: {sorted(_LAYERS)}")
    path = _LAYERS[name]
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------------------
# CHTC (UW-Madison Center for High Throughput Computing)
# --------------------------------------------------------------------------------------
# Remote locations used by chtc/make_dag.py and the submit files. These are paths too, so
# they live here. The access point is reached through the `chtc` alias in ~/.ssh/config
# (docs/getting_started.md).

#: Your NetID on CHTC; personal staging is /staging/<first letter>/<NetID>.
CHTC_USER = _need("CHTC_USER")
CHTC_ACCESS_POINT = getattr(_local, "CHTC_ACCESS_POINT", "townsend-ap4000.chtc.wisc.edu")
CHTC_HOME = f"/home/{CHTC_USER}"
CHTC_STAGING = f"/staging/{CHTC_USER[0]}/{CHTC_USER}"
#: Where deploy.sh checks this repository out on the access point.
CHTC_PROJECT_DIR = f"{CHTC_HOME}/{getattr(_local, 'CHTC_PROJECT_NAME', 'hytools-chtc')}"
#: The Apptainer image, as HTCondor addresses it: file:// plus the HasCHTCStaging
#: requirement in the submit files, because osdf:// rejected this staging directory
#: ("token rejected by the server"). Rename on every rebuild so jobs cannot pick up a
#: half-written image.
CHTC_CONTAINER = getattr(_local, "CHTC_CONTAINER", None) or f"file://{CHTC_STAGING}/hytools.sif"

#: The Townsend lab file server (EnSpec share on farnsworth) is a Pelican origin in the
#: UW Data Federation under the namespace /fwe/townsend/Enspec. Jobs write to it with
#: pelican:// URLs and CHTC's credmon supplies the token; humans see the same files on a
#: mounted drive. Missing directories are created on write; existing objects are NEVER
#: overwritten (docs/traps.md).
UWDF_FEDERATION = getattr(_local, "UWDF_FEDERATION", "chtc.wisc.edu")
UWDF_ENSPEC_NAMESPACE = f"pelican://{UWDF_FEDERATION}/fwe/townsend/Enspec"
#: Your project folder on the share: as a pelican URL for jobs, and as a local mount for
#: reading the results (ledger.py, compare_rerun.py).
UWDF_PROJECT_DIR = f"{UWDF_ENSPEC_NAMESPACE}/{_need('ENSPEC_PROJECT_SUBDIR')}"
ENSPEC_PROJECT_DIR = Path(_need("ENSPEC_PROJECT_MOUNT"))

#: Where each stage drops its tarballs on the share.
CHTC_TRAIT_MAP_DEST = f"{UWDF_PROJECT_DIR}/30_neon_aop__trait_maps"
CHTC_CORRECTION_DEST = f"{UWDF_PROJECT_DIR}/20_neon_aop__hytools_correction"


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def validate_paths() -> dict[str, bool]:
    """Report whether each root exists. Run this before a long job, not after it."""
    checks = {
        "PROJECT_ROOT": PROJECT_ROOT,
        "DATA_ROOT": DATA_ROOT,
        "TRAIT_MODEL_DIR": TRAIT_MODEL_DIR,
        "ENSPEC_PROJECT_DIR": ENSPEC_PROJECT_DIR,
        **{f"layer:{name}": path for name, path in _LAYERS.items()},
    }
    return {name: path.exists() for name, path in checks.items()}
