#!/bin/bash
# DAGMan POST script of the archive node (runs on the access point): once the archive job
# has uploaded the samples to the lab server, delete the per-line sample tarballs from
# runs/<flight>/samples/. The coefficients stay, stage C still reads them from here.
# Usage: post_archive.sh <run_dir> <archive job return value>
RUN_DIR="$1"; RET="$2"
if [ "$RET" != "0" ]; then
    echo "archive job returned $RET; keeping $RUN_DIR/samples" >&2
    exit "$RET"
fi
rm -f "$RUN_DIR"/samples/sample_*.tar
echo "samples archived $(date -u +%FT%TZ)" > "$RUN_DIR/samples/ARCHIVED"
exit 0
