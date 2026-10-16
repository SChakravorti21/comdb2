#!/bin/bash
# v4 at 16 clients with the cache flusher (memp trickle) set to keep 99%
# (the default), 50% and 1% of the cache clean, to see whether it causes the
# slow v4 inserts. At 1% the flusher has nothing to do once a few pages of the
# cache are clean.
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: $0 <outdir>}
for pct in 99 50 1; do
    EXTRA_LRL="setattr MEMPTRICKLEPERCENT $pct" CACHESTAT=1 BATCH=1 \
        "$HERE/run_matrix.sh" "$OUT/trickle$pct" 16 4000000 1 "16" "v4" "pk"
done
