#!/bin/bash
# Archive job: pack the flight's per-line sample tarballs into one samples_<flight>.tar
# (no compression, they are gzip inside) next to the coeffs tarball, which HTCondor then
# uploads to the lab server. No code bundle, no NEON download.
# Usage: run_archive.sh <flight_id>
set -euo pipefail
FLIGHT="$1"
echo "archive start $(date -u +%FT%TZ) on $(hostname)"
ls -la sample_*.tar "coeffs_${FLIGHT}.tar"
tar -cf "samples_${FLIGHT}.tar" sample_*.tar
rm -f sample_*.tar
ls -la "samples_${FLIGHT}.tar" "coeffs_${FLIGHT}.tar"
echo "archive end $(date -u +%FT%TZ)"
