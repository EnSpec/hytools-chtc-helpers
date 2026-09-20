# Resource requests

The numbers below come from the resource usage recorded in the job logs of several
thousand finished jobs. `make_dag.py` writes them into each node's `VARS`. The submit
files raise a request after each hold for exceeding it (using `NumHoldsByReason`, see
[traps.md](traps.md)) and have `periodic_release` on hold codes 34 and 21, so a job that
runs out of memory or disk is retried with double and then triple the request.

Requesting too little costs one restart. Requesting too much can leave jobs idle for a
long time, because the slots are partitionable and a request that no single machine can
satisfy never matches. In our experience over-requesting is the more expensive mistake.

| Stage | Memory | Disk | Notes |
| :--- | :--- | :--- | :--- |
| A, sample | `1.5 x` HDF5 GB, minimum 4 GB | `1.5 x` + 3 GB, minimum 8 GB | peaks at 3.2-5.9 GB on 5-7 GB lines |
| B, fit BRDF | `0.08 x` the flight's total HDF5 GB, minimum 8 GB | `0.06 x` + 4 GB | see below |
| C, trait maps | 12 GB flat (`0.15 x` above 80 GB) | `3.7 x` + 8 GB, minimum 12 GB | see below |
| archive | 1 GB | `0.06 x` + 2 GB | only repacks tarballs |

## Stage C memory does not depend on the line size

Stage C works on one 96 x 1024 pixel window at a time, so the size of the line makes
little difference. Over 1 200 jobs the peak was 2.5-4.8 GB (median) and 9.5 GB at worst,
for HDF5 sizes from 1 to 20 GB. A flat 12 GB covers that.

Disk does scale with the line, since the HDF5, its pixel-major copy (for 2022+ files), the
GeoTIFF and the tarball are all on scratch at the same time.

The 96-row window was also chosen from measurements; 256 rows went past 8 GB.

## Stage B pools all lines of a flight

The BRDF fit holds 2 % of every line's pixels in memory as float32 over the good bands.
Peak memory was about `0.04 x` the flight's total HDF5 size (6.2 GB for a 156 GB flight),
so the rule asks for twice that. An earlier `0.30 x` rule requested up to 78 GB and sat
idle for 12 hours because no partitionable slot had that much free at once.

If a stage B job dies within seconds with an empty `.err` and is held for a missing
`coeffs_<flight>.tar`, it was killed by the cgroup. SIGKILL leaves no traceback, and
`set -e` in the wrapper turns it into a transfer failure (hold code 12) rather than a
memory hold (34), so nothing releases it automatically. `run_fit_brdf.sh` now prints the
exit code (`exited 137`) to make this recognisable.

## Throughput

On a full pool our fair share was about 150 concurrent jobs, which came to 46-60
flightlines per hour end to end. Queueing more does not speed things up; when
`condor_q -better-analyze` reports `PREEMPTION_REQUIREMENTS == False` you already have
your share.

Per line: 45-120 s to download 4-13 GB from NEON, about 5 minutes for stage A, and 20-40
minutes for stage C at roughly 230 s per megapixel.
