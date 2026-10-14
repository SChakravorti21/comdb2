#!/bin/bash
# The full benchmark as reported in the README.
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: $0 <outdir>}
KEYS="int_seq int_rand seq16 v7 v4"
"$HERE/run_matrix.sh" "$OUT/main"    64   1500000 3 "1 4 16 64" "$KEYS" "pk pk_sec"
"$HERE/run_matrix.sh" "$OUT/incache" 4096 1500000 1 "1 4 16 64" "$KEYS" "pk pk_sec"
