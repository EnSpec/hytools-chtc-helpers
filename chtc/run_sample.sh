#!/bin/bash
# Stage A job: one flightline -> topo coefficients + BRDF samples, packed as
# sample_<line>.tar for HTCondor to carry back.
# Usage: run_sample.sh <flight_id> <line_stem>
FLIGHT="$1"; LINE="$2"
source job_common.sh

python src/20_neon_aop__hytools_correction/1_sample_line.py \
    --flight "$FLIGHT" --line "$LINE" \
    --out-dir out --delete-h5

tar -cf "sample_${LINE}.tar" -C out .
ls -la out "sample_${LINE}.tar"
echo "job end $(date -u +%FT%TZ)"
