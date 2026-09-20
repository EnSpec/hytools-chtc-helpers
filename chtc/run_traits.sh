#!/bin/bash
# Stage C job: one flightline -> trait maps (GeoTIFF), packed as traits_<line>.tar.
# Usage: run_traits.sh <flight_id> <line_stem>
# Inputs besides the code bundle: coeffs_<flight>.tar (topo + BRDF coefficients for every
# line of the flight) and trait_models.tar.gz (the PLSR model json files). The HDF5 is
# downloaded by the script and deleted before the job ends.
FLIGHT="$1"; LINE="$2"
source job_common.sh

mkdir -p coeffs models
tar -xf "coeffs_${FLIGHT}.tar" -C coeffs
tar -xzf trait_models.tar.gz -C models

python src/30_neon_aop__trait_maps/1_map_traits_line.py \
    --flight "$FLIGHT" --line "$LINE" \
    --coeff-dir coeffs --models models --out-dir out --delete-h5

# The sidecar also travels outside the tarball, so chtc/ledger.py can read versions and
# timings on the share without opening a 600 MB archive.
cp "out/${LINE}_traits.json" .
# Pack, then delete the originals. Not `tar --remove-files -C out .`: GNU tar then tries
# to rmdir "." and exits non-zero, which makes HTCondor retry every finished job, and the
# retry's upload fails because the objects already exist (docs/traps.md).
tar -cf "traits_${LINE}.tar" -C out . && rm -rf out
ls -la "traits_${LINE}.tar" "${LINE}_traits.json"
echo "job end $(date -u +%FT%TZ)"
