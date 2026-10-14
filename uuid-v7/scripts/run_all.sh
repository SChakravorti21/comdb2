#!/bin/bash
# The full benchmark as reported in the README: single-row inserts, 16MB cache.
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: $0 <outdir>}
KEYS="int_seq int_rand v7 v4 nextseq nextseq_v7 nextseq_v4"
BATCH=1 "$HERE/run_matrix.sh" "$OUT/main" 16 500000 3 "1 4 16 64" "$KEYS" "pk"
