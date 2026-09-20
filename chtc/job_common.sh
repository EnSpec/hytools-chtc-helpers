#!/bin/bash
# Sourced by every job wrapper. Unpacks the code bundle into the job scratch directory,
# points config/paths.py at a data root inside scratch, and exports the NEON token.
# Nothing here touches /home or /staging directly: HTCondor moves files in and out.

set -euo pipefail

JOB_ROOT="$PWD"
export JOB_ROOT
echo "job start $(date -u +%FT%TZ) on $(hostname), scratch $JOB_ROOT"

# One core per job, so one BLAS thread. HTCondor already exports OMP_NUM_THREADS and
# OPENBLAS_NUM_THREADS equal to request_cpus; this repeats it so a
# wrapper run outside HTCondor behaves the same (the container's OpenBLAS otherwise
# starts one thread per host core, and 32 spinning threads on one core make a 0.4 s
# matrix product take 50 s).
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1

tar -xzf code.tar.gz                      # -> config/ src/
mkdir -p data
cat > config/local_paths.py <<EOF
DATA_ROOT = r"$JOB_ROOT/data"
EOF

if [[ -f neon_token.txt ]]; then
    NEON_API_TOKEN="$(tr -d '\r\n ' < neon_token.txt)"
    export NEON_API_TOKEN
fi

# The flight manifest arrives as a top-level input file; put it where the scripts look.
mkdir -p data/10_neon_aop__flightline_inventory/flights
cp "${FLIGHT}.json" data/10_neon_aop__flightline_inventory/flights/

mkdir -p out
