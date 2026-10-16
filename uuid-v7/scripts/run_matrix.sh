#!/bin/bash
# Run the benchmark matrix on the benchmark host against a freshly created
# database. Workload order is reshuffled every repetition so that host drift
# does not line up with one key type.
#
#   run_matrix.sh <outdir> <cache_mb> <rows> <reps> "<threads>" "<keys>" "<shapes>"
# Rows per transaction comes from $BATCH (default 1). With CACHESTAT=1, the
# server's cache and log stats are also sampled every second into
# ts/<run>.stat.txt.
set -e

OUT=$1 CACHE_MB=$2 ROWS=$3 REPS=$4 THREADS=$5 KEYS=$6 SHAPES=$7
[ -n "$SHAPES" ] || { echo "usage: $0 outdir cache_mb rows reps threads keys shapes" >&2; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
CTL=$HERE/ctl.sh
BENCH=$HERE/../bench/uuidbench
BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
DBDIR=${DBDIR:-$BASE/db}
CFG=$DBDIR/comdb2db.cfg
BATCH=${BATCH:-1}
export BASE DBDIR CACHE_MB

mkdir -p "$OUT/ts"
"$CTL" stop
"$CTL" create
"$CTL" start
"$HERE/env.sh" >> "$OUT/env.txt" 2>&1

# A second invocation on the same outdir adds runs to it, so run numbers
# continue from the runs already recorded.
RUNS=$OUT/runs.csv
seq=0
[ -f "$RUNS" ] && seq=$(($(wc -l < "$RUNS") - 1))
for rep in $(seq "$REPS"); do
    for T in $(shuf -e $THREADS); do
        for shape in $(shuf -e $SHAPES); do
            for key in $(shuf -e $KEYS); do
                seq=$((seq + 1))
                name=$(printf "%03d_%s_%s_T%s_r%s" $seq $key $shape $T $rep)
                "$CTL" files > "$OUT/.files_before"
                "$CTL" mktable "$key" "$shape"
                # Write back the previous run's dirty pages so they are not
                # flushed on this run's time.
                "$CTL" sql "exec procedure sys.cmd.send('flush')" > /dev/null
                sleep 2
                load=$(cut -d' ' -f1 /proc/loadavg)
                log0=$("$CTL" logbytes)
                if [ "$CACHESTAT" = 1 ]; then
                    while :; do
                        echo "== $(date +%s.%N)"
                        "$CTL" sql "exec procedure sys.cmd.send('bdb cachestat')" || true
                        "$CTL" sql "exec procedure sys.cmd.send('bdb logstat')" || true
                        sleep 1
                    done > "$OUT/ts/$name.stat.txt" 2>&1 &
                    sampler=$!
                fi
                "$BENCH" -d uuidbench -c "$CFG" -k "$key" -n "$ROWS" -T "$T" -b "$BATCH" \
                    -o "$OUT/ts/$name" -L "$name" 2> "$OUT/ts/$name.err"
                if [ "$CACHESTAT" = 1 ]; then
                    { kill $sampler; wait $sampler; } 2> /dev/null || true
                fi
                log1=$("$CTL" logbytes)
                sizes=$("$CTL" filesizes "$OUT/.files_before")
                if [ ! -f "$RUNS" ]; then
                    echo "seq,rep,shape,cache_mb,loadavg_before,log_bytes,data_bytes,pk_index_bytes,second_index_bytes,$(head -1 "$OUT/ts/$name.summary.csv")" > "$RUNS"
                fi
                echo "$seq,$rep,$shape,$CACHE_MB,$load,$((log1 - log0)),$sizes,$(tail -1 "$OUT/ts/$name.summary.csv")" >> "$RUNS"
            done
        done
    done
done
"$CTL" stop
