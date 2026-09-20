#!/bin/bash
# Stage B job: one flight -> BRDF coefficients from all its sample files. The per-line
# sample_<line>.tar files arrive as inputs; the result leaves as coeffs_<flight>.tar.
# Usage: run_fit_brdf.sh <flight_id>
FLIGHT="$1"
source job_common.sh

mkdir -p samples
for t in sample_*.tar; do tar -xf "$t" -C samples; done
echo "pooled $(ls samples/*_prebrdf_sample.h5 | wc -l) sample files"

# The fit pools every line's samples into memory; a big flight can be OOM-killed, which
# arrives as SIGKILL with no traceback. set -e (from job_common) would then abort silently
# and leave no coeffs tar, so run it guarded and report the code before exiting on it.
set +e
python src/20_neon_aop__hytools_correction/2_fit_brdf.py --flight "$FLIGHT" --dir samples
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    echo "fit_brdf exited $rc (137 = killed, most likely out of memory: raise fit memory)" >&2
    exit "$rc"
fi

tar -cf "coeffs_${FLIGHT}.tar" -C samples $(cd samples && ls *_brdf_coeffs.json *_topo_coeffs.json brdf_fit_summary.json)
ls -la "coeffs_${FLIGHT}.tar"
echo "job end $(date -u +%FT%TZ)"
