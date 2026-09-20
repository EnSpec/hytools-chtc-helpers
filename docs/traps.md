# Known problems

These are things that are not obvious from the code and that mostly only showed up once
we were running thousands of jobs. Most of them took a while to track down.

## NEON files changed their chunking in 2022

* 2013-2021 files are named `..._<date>_<time>_reflectance.h5` and use small 3-D chunks,
  for example `(424, 27, 14)`.
* Files from 2022 on are named `..._Lnnn-n_<date>_directional_reflectance.h5` and store
  one whole band per chunk, `(lines, columns, 1)`.

With band-sized chunks, any window read that spans bands (`get_chunk`,
`iterate(by='chunk')`, every pass of the trait mapping) has to decompress every band
chunk, in other words the whole cube, for each window. On a 0.8 GB line a 60 x 60 pixel
window took 3.3 s, the same as a full-width strip. Trait mapping went from about 230 s to
about 900 s per megapixel, and anything that reads many small windows goes from minutes
to hours.

The workaround is in `src/00_common/hdf5_cache.py`: the cube is written once as an
uncompressed pixel-major memmap next to the HDF5 (reading 512 MB of bands at a time, so
each chunk is decompressed once, roughly 11 s per GB), and `get_chunk` then reads from
that. The results are bitwise identical. It costs about twice the HDF5 size in scratch
disk, which is why stage C requests `3.7 x` the HDF5 size.

Reads of single bands (`get_band`, `ndi()`, the mask functions) touch one chunk each and
are not affected.

## The share does not overwrite

Pelican refuses to replace an existing object. A job whose upload succeeded and which then
runs again (after a non-zero exit, a hold and release, or a DAG resubmission) gets held
with `remote object already exists`.

* Before rerunning anything, move the old outputs out of the way rather than deleting
  them. We use `z_archive_<date>_<reason>/` with the same folder structure.
* To let a DAG finish past a line you have already checked, add `DONE <node>` to
  `<flight>.dag.rescue001` and resubmit without `-f`.

### An "already exists" hold can hide a corrupt file

Twice out of 8 264 tarballs, the first upload produced a file of the right size but
containing only zeros, the retry was refused, and the DAG carried on. `tar -tf` does not
necessarily catch this. After any "already exists" hold it is worth checking the file
properly: a valid tar has `ustar` at byte offset 257, and every band of the GeoTIFF
should read through rasterio. `chtc/compare_rerun.py` does this for rerun lines.

## Hold codes

| Code | Meaning | Released automatically? |
| :--- | :--- | :--- |
| 34 | over the memory request | yes, `periodic_release` retries with a larger request, up to three starts |
| 21 | over the disk request | yes, same |
| 12 | output transfer failed | no, read `HoldReason` |

A released job can be held again within seconds because the job ad still carries
`DiskUsage` and `MemoryUsage` from the previous run, and the startd checks those before
the new run has reported anything. `condor_qedit <id> DiskUsage 1024` (and
`ResidentSetSize`), then release. The `periodic_release` expressions in the submit files
avoid this by raising the request instead.

## Memory and disk requests should grow separately

An earlier version of the submit files multiplied both requests by `NumJobStarts + 1`. A
job that was held twice for memory then also asked for three times the disk, which for a
35 GB line came to 232 GB of scratch, and nothing could match it. Memory now grows with
`NumHoldsByReason.JobOutOfResources` (code 34) and disk with
`NumHoldsByReason.StartdHeldJob` (code 21). If you reuse the pattern, keep that part.

## Large lines and the pixel-major copy

NEON lines range from about 1 to 35 GB. The copy originally read 32 bands at a time,
which is 5 GB on a 35 GB line and went over the memory limit. `pixel_major_copy` now
sizes the block to 512 MB (`COPY_BLOCK_BYTES`).

## Files without a `Units` attribute on the wavelengths

`open_neon` reads `Spectral_Data/Wavelength.attrs['Units']` with no default, so a file
written without that attribute (we saw one 2016 CHEQ line) fails to open at all.
`hdf5_cache.patch_neon_reader()`, which is called before every `read_file`, returns
`"nanometers"` for that one key. Expect other small structural differences in the
2015-2017 files; they do not follow year boundaries, so testing one old flight does not
cover the rest.

## Do not use `condor_submit_dag -f`

`-force` renames `<dag>.rescue001` to `.old` and reruns every node, and it is passed down
to SUBDAGs, so a single `-f` can redo a whole batch, after which every rerun hits the
"already exists" problem above. If the submit complains that `.condor.sub`, `.lib.out` or
`.dagman.log` already exist, delete those files and submit again without `-f`.

## A held DAGMan takes five minutes to come back

If you hold the DAGMan jobs (for example `condor_hold -constraint 'JobUniverse == 7'` to
deploy new code), each one exits with status 3 and the schedd only restarts it in recovery
mode after `SCHEDULER_UNIVERSE_COOL_DOWN_DURATION`, which is five minutes. During that
time every flight looks broken. Running jobs are not affected. Wait six minutes before
doing anything.

DAGMan reads a SUBDAG file only when it submits that SUBDAG, so regenerating the DAG of a
flight that has not started yet is safe.

## Jobs stay idle although the pool has free slots

`condor_q -better-analyze <id>` usually tells you which case you are in:

* "0 slots match, N would match if drained": the slots are partitionable and none has
  that much free at once. This usually means the request is too large. At one point a
  "2x the HDF5 size" memory rule for stage C asked for 44 GB and left 96 jobs idle for 12
  hours while 259 slots were free. See [resources.md](resources.md).
* `PREEMPTION_REQUIREMENTS == False`: the pool is full and you are getting your fair
  share. Queueing more jobs does not help.

## A flightline with no vegetation crashes stage A

HyTools' `ndvi_stratify` indexes with the empty float array that `np.random.choice`
returns for an empty population, so a line where no pixel passes the BRDF mask (NDVI
0.1-1, slope, cos i) raises `IndexError`. We hit this on a shortgrass CPER line.
`1_sample_line.py` catches it and writes the topo coefficients without BRDF samples; the
other lines of the flight provide the fit. Fixed upstream in EnSpec/hytools#30.

## NEON lists co-located sites twice

Some sites are flown inside another site's flight box and the NEON API lists the same
files under both site codes: STEI + TREE, WOOD + DCFS, KONZ + KONA. Counting naively
double-counts 77 flights. `1_build_inventory.py` de-duplicates by file name.

`CHEQ` (D05, files belong to STEI) and `BRDF` (D10 2020, CPER) are flight-box names
rather than API sites, and signed-URL requests for them return HTTP 400. The manifests
carry `api_site` for those cases. Note that a line named STEI can lie entirely over TREE.

## Smaller things

* DAG `VARS` names must not start with `REQUEST_`. `condor_submit` treats any
  `request_<x>` macro as a resource request, so `REQUEST_MEMORY_MB` became a demand for a
  machine resource called `MEMORY_MB` and nothing matched.
* `tar --remove-files -C out .` exits non-zero because tar then tries to `rmdir "."`, and
  HTCondor retries a job whose outputs were already uploaded. Pack first, then `rm -rf`.
* "Memory (MB)" in a short job's log is the last five-minute sample, not the peak.
* HTCondor sets `OMP_NUM_THREADS` and `OPENBLAS_NUM_THREADS` to `request_cpus`. Outside
  HTCondor you have to set them yourself, otherwise OpenBLAS starts one thread per host
  core and a 0.4 s matrix product can take 50 s.
