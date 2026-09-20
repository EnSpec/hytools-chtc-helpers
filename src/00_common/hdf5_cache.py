"""HDF5 access tuning for NEON reflectance files: chunk cache, keep-open, and a
pixel-major copy for the band-chunked files NEON has written since 2022.

NEON stores Reflectance_Data as int16 gzip chunks of (93 rows, 29 cols, 27 bands). HyTools
reads one band at a time, so every band read decompresses one 27-band chunk group of the
whole image, and with h5py's default 1 MB chunk cache the next band decompresses the same
chunks again. Measured on a 1.4 GB BLUE line: 54 consecutive band reads take 24 s with
the default cache and 1.8 s with a 2 GB cache.

HyTools opens files with a bare ``h5py.File(name, 'r')``, so the only way to change the
cache without forking is to replace ``h5py.File`` with a subclass whose defaults are
larger. ``install()`` does that once, process-wide, before HyTools opens anything.

Files from 2022 on (the `directional_reflectance` naming) are chunked one whole band per
chunk ((lines, columns, 1), or (837, 837, 1) tiles in 2026), so any window read across the spectrum decompresses every
band: a 60 x 60 window of a 0.8 GB line took 3.3 s, a 96-row strip the same, and no
chunk cache can help once the cube is larger than memory. Stage C on such lines ran
four times slower than on 2013-2021 files, and anything that reads many small windows
takes hours instead of minutes. ``band_major()``
detects the layout, ``pixel_major_copy()`` writes the cube once as an uncompressed
(lines, columns, bands) memmap next to the HDF5 (11 s per GB; each band chunk is read
exactly once) and ``use_pixel_major()`` makes ``get_chunk`` read windows from it
(1 ms). Band reads (``get_band``, masks, stage A) stay on the HDF5, where a band is one
chunk and cheap.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import h5py
import numpy as np

_ORIGINAL_FILE = h5py.File


class _CachedFile(_ORIGINAL_FILE):
    """h5py.File with a large raw-data chunk cache unless the caller sets its own."""

    _defaults: dict = {}

    def __init__(self, *args, **kwargs):
        for key, value in self._defaults.items():
            kwargs.setdefault(key, value)
        super().__init__(*args, **kwargs)


def patch_neon_reader() -> None:
    """Let HyTools open a NEON file whose wavelengths carry no ``Units`` attribute.

    ``hytools.io.neon.open_neon`` reads ``Spectral_Data/Wavelength.attrs['Units']``
    unconditionally and raises ``KeyError`` when NEON did not write it, which aborts the
    whole read so the flightline cannot be processed at all. Seen on
    ``NEON_D05_CHEQ_DP1_20160912_160540_reflectance.h5``. The value only
    ever reaches an ENVI header, which nothing here writes, and every NEON
    wavelength array is in nanometres, so that is a safe default.

    The patch replaces h5py's attribute lookup for the duration of one ``open_neon``
    call and only for the key ``Units``; every other missing attribute raises as before.
    Idempotent. ``base.py`` does ``from .io.neon import open_neon``, so the name has to be
    replaced on ``hytools.base``, not on ``hytools.io.neon``.
    """
    import hytools.base as _base  # here, so this module stays importable on its own

    if getattr(_base.open_neon, "_units_default_patched", False):
        return
    original = _base.open_neon

    def open_neon(hy_obj, no_data=-9999):
        from h5py._hl.attrs import AttributeManager

        original_getitem = AttributeManager.__getitem__

        def getitem(self, name):
            try:
                return original_getitem(self, name)
            except KeyError:
                if name == "Units":
                    return "nanometers"
                raise

        AttributeManager.__getitem__ = getitem
        try:
            return original(hy_obj, no_data)
        finally:
            AttributeManager.__getitem__ = original_getitem

    open_neon._units_default_patched = True
    _base.open_neon = open_neon


def keep_open(hy) -> None:
    """Open a HyTools object's HDF5 once and keep it open across band reads.

    HyTools' get_band/get_chunk call load_data() and close_data() around every read,
    which throws the chunk cache away each time; with the file held open the 2 GB
    cache from install() is reused. Call after hy.read_file(), and release() at the end.
    """
    hy.load_data()
    hy.load_data = lambda mode="r": None
    hy.close_data = lambda: None


def release(hy) -> None:
    """Undo keep_open() and close the file."""
    if hasattr(hy, "hdf_obj") and hy.hdf_obj is not None:
        hy.hdf_obj.close()
        hy.hdf_obj = None
    hy.data = None
    for name in ("load_data", "close_data"):
        hy.__dict__.pop(name, None)


def band_major(h5_path: str | Path) -> bool:
    """True when the reflectance cube is chunked one band (or one band tile) per chunk."""
    with _ORIGINAL_FILE(h5_path, "r") as f:
        base = next(k for k in f.keys() if "Reflectance" in f[k])
        chunks = f[base]["Reflectance"]["Reflectance_Data"].chunks
    return chunks is not None and chunks[2] == 1


def pixel_major_path(h5_path: str | Path) -> Path:
    return Path(str(h5_path) + ".bip")


#: Largest read block pixel_major_copy allocates at once, before the memmap write.
COPY_BLOCK_BYTES = 512 << 20


def pixel_major_copy(h5_path: str | Path, bands_per_pass: int | None = None) -> Path:
    """Write the cube as an uncompressed (lines, columns, bands) memmap next to the HDF5.

    Reads whole-band chunks a few at a time (each chunk exactly once) and writes them into
    their band slots of the memmap; the OS page cache absorbs the strided writes. A
    finished copy (its ``.json`` sidecar present and the size right) is reused.

    ``bands_per_pass`` defaults to as many bands as fit in ``COPY_BLOCK_BYTES``, at least
    one. A fixed 32 was fine for a typical 5 GB line but allocated 5 GB on a 35 GB one
    (DEJU 2023 L021-1), which the job's memory limit killed before it wrote anything.
    """
    h5_path = Path(h5_path)
    bip = pixel_major_path(h5_path)
    meta = bip.with_suffix(".bip.json")
    with _ORIGINAL_FILE(h5_path, "r") as f:
        base = next(k for k in f.keys() if "Reflectance" in f[k])
        d = f[base]["Reflectance"]["Reflectance_Data"]
        shape, dtype = tuple(int(x) for x in d.shape), np.dtype(d.dtype)
        nbytes = int(np.prod(shape)) * dtype.itemsize
        if meta.exists() and bip.exists() and bip.stat().st_size == nbytes:
            return bip
        if bands_per_pass is None:
            band_bytes = shape[0] * shape[1] * dtype.itemsize
            bands_per_pass = max(1, min(32, COPY_BLOCK_BYTES // band_bytes))
        tmp = bip.with_suffix(".bip.tmp")
        mm = np.memmap(tmp, dtype=dtype, mode="w+", shape=shape)
        for b0 in range(0, shape[2], bands_per_pass):
            mm[:, :, b0:b0 + bands_per_pass] = d[:, :, b0:b0 + bands_per_pass]
        mm.flush()
        del mm
    os.replace(tmp, bip)
    meta.write_text(json.dumps({"shape": shape, "dtype": dtype.str, "source": h5_path.name}))
    return bip


def use_pixel_major(hy, bip: str | Path) -> None:
    """Make ``hy.get_chunk`` read windows from the pixel-major copy instead of the HDF5.

    Everything else (corrections, resampling, masks, band reads) is unchanged, so the
    result is identical to HyTools' own ``get_chunk``; only the read is different.
    """
    from hytools.transform.resampling import apply_resampler

    meta = json.loads(Path(bip).with_suffix(".bip.json").read_text())
    mm = np.memmap(bip, dtype=np.dtype(meta["dtype"]), mode="r", shape=tuple(meta["shape"]))

    def get_chunk(col_start, col_end, line_start, line_end, corrections=[], resample=False):  # noqa: B006
        chunk = np.array(mm[line_start:line_end, col_start:col_end, :])
        chunk = hy.correct(chunk, "chunk", [col_start, col_end, line_start, line_end], corrections)
        if resample:
            chunk = apply_resampler(hy, chunk[:, :, ~hy.bad_bands])
        return chunk

    hy.get_chunk = get_chunk
    hy.pixel_major = mm


def remove_pixel_major(h5_path: str | Path) -> None:
    for p in (pixel_major_path(h5_path), pixel_major_path(h5_path).with_suffix(".bip.json")):
        Path(p).unlink(missing_ok=True)


def install(nbytes: int = 2 << 30, nslots: int = 200_003) -> None:
    """Make every later h5py.File (HyTools included) use an ``nbytes`` chunk cache.

    ``nslots`` is the hash-table size and should be a prime well above the number of
    chunks kept; 200 003 is plenty for a 10 GB flightline.
    """
    _CachedFile._defaults = {"rdcc_nbytes": nbytes, "rdcc_nslots": nslots, "rdcc_w0": 0.75}
    h5py.File = _CachedFile
