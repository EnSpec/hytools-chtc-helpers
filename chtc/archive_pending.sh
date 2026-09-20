#!/bin/bash
# Back-fill the archive node: for every flight under chtc/runs/ that has its coefficients
# and sample tarballs but no samples/ARCHIVED marker and no DAG in the queue, write the
# archive-only DAG and submit it. Runs on the access point; safe to rerun any time.
#   bash chtc/archive_pending.sh          # submit
#   bash chtc/archive_pending.sh --dry    # only list
cd "$(dirname "$0")/.." || exit 1
dry="${1:-}"
running=$(condor_q -constraint 'JobUniverse == 7' -af Cmd Args Iwd 2>/dev/null)
n=0
for d in chtc/runs/NEON_*/; do
    f=$(basename "$d")
    [ -f "$d/coeffs_$f.tar" ] || continue
    [ -f "$d/samples/ARCHIVED" ] && continue
    ls "$d"/samples/sample_*.tar > /dev/null 2>&1 || continue
    if echo "$running" | grep -q "$f"; then echo "skip $f: a DAG of it is in the queue"; continue; fi
    echo "archive $f ($(ls "$d"/samples/sample_*.tar | wc -l) sample tars)"
    n=$((n + 1))
    [ "$dry" = "--dry" ] && continue
    python3 chtc/make_dag.py "$f" --archive-only > /dev/null
    (cd "$d" && condor_submit_dag "${f}_archive.dag" 2>&1 | grep "^ERROR")
done
echo "$n flights to archive"
