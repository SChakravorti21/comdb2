#!/bin/bash
# The full benchmark as reported in the README: single-row inserts, 16MB cache,
# 4M rows so that the random-key PK indexes end up about 10x the cache.
# 1 client is left out because at this size it would not finish overnight.
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: $0 <outdir>}
KEYS="int_seq int_rand v7 v4 nextseq nextseq_v7 nextseq_v4"
BATCH=1 "$HERE/run_matrix.sh" "$OUT/main" 16 4000000 3 "4 16 64" "$KEYS" "pk"
# 8 clients matches maxwt, the number of transactions the master applies at once.
# It was added in a second pass, which recreates the database.
BATCH=1 "$HERE/run_matrix.sh" "$OUT/main" 16 4000000 3 "8" "$KEYS" "pk"
# v4 and v7 only, five times as many rows, to see whether v4's lead keeps
# shrinking as its index grows; 64 clients tracked 16 within 3% at 4M rows.
BATCH=1 "$HERE/run_matrix.sh" "$OUT/long" 16 20000000 3 "16" "v4 v7" "pk"
