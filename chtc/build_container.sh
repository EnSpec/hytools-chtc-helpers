#!/bin/bash
# Runs on a CHTC build node: apptainer build <name>.sif hytools.def
set -uo pipefail
NAME="$1"
echo "build start $(date -u +%FT%TZ) on $(hostname), pwd $PWD"
apptainer --version
echo "TMPDIR=${TMPDIR:-unset} HOME=$HOME"
mkdir -p "$PWD/apptainer_tmp" "$PWD/apptainer_cache" "$PWD/home"
export APPTAINER_TMPDIR="$PWD/apptainer_tmp" APPTAINER_CACHEDIR="$PWD/apptainer_cache"
# The build node has no /home/<user>; apptainer's build stage 2 needs a real HOME.
export HOME="$PWD/home"

echo "--- attempt 1: plain build (verbose)"
apptainer -v build "$NAME" hytools.def; s1=$?
echo "exit $s1"
if [[ $s1 -ne 0 ]]; then
    echo "--- attempt 2: build --fakeroot"
    apptainer -v build --fakeroot "$NAME" hytools.def; s2=$?
    echo "exit $s2"
fi
if [[ ! -f "$NAME" ]]; then
    echo "--- attempt 3: pull the base image only"
    apptainer -v pull base.sif docker://python:3.11-slim; echo "exit $?"
    ls -la
    exit 1
fi
ls -la "$NAME"
apptainer exec "$NAME" python -c "import hytools, rasterio, h5py, numpy, requests; print('container ok')"
rm -rf "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
echo "build end $(date -u +%FT%TZ)"
