"""NEON's ATCOR haze / cloud / water classification, the only cloud information kept.

Every DP1.30006.001 file carries ``Reflectance/Metadata/Ancillary_Imagery/Haze_Cloud_Water_Map``
(uint8, one of the 22 ATCOR classes below; HyTools' ``anc_path`` spells it
``Haze_Water_Cloud_Map``, which does not exist). The pipeline does not screen clouds in the
topo/BRDF fit (hytools_config.py follows Zhiwei Ye's settings, which do not either), but
the reflectance is thrown away after each job, so this map is read once per line and
carried into the trait-map QA raster to filter by later.

``Cast_Shadow`` is deliberately not used: in the 2017 BLUE and 2019 CLBJ files it is 1
on every valid pixel and 241 on nodata, i.e. a placeholder. Class 1 of this map is the
shadow flag ATCOR actually used.
"""

from __future__ import annotations

import h5py
import numpy as np

DATASET = "Haze_Cloud_Water_Map"
#: Value written where the layer is absent from a file.
MISSING = 255

CLASS_NAMES = {
    0: "geocoded background", 1: "shadow/topogr. shadow", 2: "thin cirrus (water)",
    3: "medium cirrus (water)", 4: "thick cirrus (water)", 5: "land (clear)", 6: "saturated",
    7: "snow/ice (ice cloud)", 8: "thin cirrus (land)", 9: "medium cirrus (land)",
    10: "thick cirrus (land)", 11: "thin haze (land)", 12: "medium haze (land)",
    13: "thin haze/glint (water)", 14: "med. haze/glint (water)", 15: "cloud (land)",
    16: "cloud (water)", 17: "water", 18: "cirrus cloud", 19: "cirrus cloud (thick)",
    20: "bright", 21: "topogr. shadow",
}
CLEAR_LAND = {5}
SHADOW = {1, 21}
CLOUD = {2, 3, 4, 7, 8, 9, 10, 15, 16, 18, 19}
HAZE = {11, 12, 13, 14}
WATER = {17}


def read_class_map(h5_path, base_key: str) -> np.ndarray | None:
    """The full-image uint8 class map, or None when the file has no such layer."""
    with h5py.File(h5_path, "r") as f:
        anc = f[base_key]["Reflectance"]["Metadata"]["Ancillary_Imagery"]
        if DATASET not in anc:
            return None
        return anc[DATASET][()].astype(np.uint8)


def isin(classes: np.ndarray, members: set[int]) -> np.ndarray:
    return np.isin(classes, list(members))
