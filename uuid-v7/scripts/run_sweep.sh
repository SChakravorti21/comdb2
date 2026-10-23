#!/bin/bash
# v4 vs v7 single-row inserts at 16 clients across cache sizes, with the PK
# index kept at about 12x the cache (4M rows per 16MB, as in the original 16MB
# runs), and v4 again under each buffer-allocator prototype switch.
#
#   run_sweep.sh <outdir> [preload|measure|all]
#
# Preload grows one table per key with 100-row transactions and snapshots it at
# each size. Each measured run starts a server with that cache size on a copy
# of the snapshot and inserts 10% more rows (at least 1M) one row at a time,
# sampling the server's cache and log stats every second over one connection.
set -e

OUT=$1
PHASE=${2:-all}
HERE=$(cd "$(dirname "$0")" && pwd)
CTL=$HERE/ctl.sh
BENCH=$HERE/../bench/uuidbench
BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
export BUILD=${BUILD:-$BASE/build-proto}
SIZES=${SIZES:-16 64 256 1024}
ROWS_PER_MB=${ROWS_PER_MB:-250000}
MIN_MEASURED=${MIN_MEASURED:-1000000}
PRELOAD_CACHE_MB=16384
T=16
mkdir -p "$OUT/ts"

rows_for() { echo $(($1 * ROWS_PER_MB)); }
measured_rows() { local r=$(($(rows_for "$1") / 10)); echo $((r < MIN_MEASURED ? MIN_MEASURED : r)); }

# Cache size (MB) is fixed at create time in the lrl; rewrite it for each run.
set_cache() {
    local lrl=$DBDIR/uuidbench/uuidbench.lrl
    sed -i "s/^cache .*/cache $1 mb/; s/^cachekbmin .*/cachekbmin $(($1 * 1024))/" "$lrl"
}

sample_stats() {
    while :; do
        echo "select '== $(date +%s.%N)'"
        echo "exec procedure sys.cmd.send('bdb cachestat')"
        echo "exec procedure sys.cmd.send('bdb logstat')"
        sleep 1
    done | "$BUILD/tools/cdb2sql/cdb2sql" -c "$DBDIR/comdb2db.cfg" -tabs uuidbench local - 2>&1
}

preload() {
    local key=$1 have=0
    export DBDIR=$BASE/db-sweep-$key CACHE_MB=$PRELOAD_CACHE_MB
    "$CTL" stop
    "$CTL" create
    "$CTL" start
    "$HERE/env.sh" >> "$OUT/env.txt" 2>&1
    "$CTL" mktable "$key" pk
    for size in $SIZES; do
        local want
        want=$(rows_for "$size")
        "$BENCH" -d uuidbench -c "$DBDIR/comdb2db.cfg" -k "$key" -n $((want - have)) -T $T -b 100 \
            -o "$OUT/ts/preload_${key}_$size" -L "preload_${key}_$size" 2> "$OUT/ts/preload_${key}_$size.err"
        have=$want
        "$CTL" sql "exec procedure sys.cmd.send('flush')" > /dev/null
        "$CTL" stop
        rm -rf "$DBDIR/snap$size"
        cp -a "$DBDIR/uuidbench" "$DBDIR/snap$size"
        (cd "$DBDIR/snap$size" && ls -l t_*) > "$OUT/snap_${key}_$size.txt"
        "$CTL" start
    done
    "$CTL" stop
}

# One run: key, variant, cache size, repetition.
measure_one() {
    local key=$1 variant=$2 size=$3 rep=$4 seq name
    export DBDIR=$BASE/db-sweep-$key CACHE_MB=$size
    case $variant in
    base) export MP_ALLOC_DIRTY_CAP=0 MP_ALLOC_UNLOCKED_SCAN=0 ;;
    cap32) export MP_ALLOC_DIRTY_CAP=32 MP_ALLOC_UNLOCKED_SCAN=0 ;;
    unlocked) export MP_ALLOC_DIRTY_CAP=0 MP_ALLOC_UNLOCKED_SCAN=1 ;;
    both) export MP_ALLOC_DIRTY_CAP=32 MP_ALLOC_UNLOCKED_SCAN=1 ;;
    *) echo "bad variant $variant" >&2; exit 2 ;;
    esac
    seq=$(($(ls "$OUT"/ts/ | grep '\.summary\.csv$' | grep -v preload | wc -l) + 1))
    name=$(printf "%03d_%s_%s_c%s_r%s" "$seq" "$key" "$variant" "$size" "$rep")
    rm -rf "$DBDIR/uuidbench"
    cp -a "$DBDIR/snap$size" "$DBDIR/uuidbench"
    set_cache "$size"
    "$CTL" start
    grep -m1 "mp_alloc prototype" "$DBDIR/server.log" > "$OUT/ts/$name.variant.txt" || true
    sample_stats > "$OUT/ts/$name.stat.txt" &
    local stats=$!
    "$BENCH" -d uuidbench -c "$DBDIR/comdb2db.cfg" -k "$key" -n "$(measured_rows "$size")" -T $T -b 1 \
        -o "$OUT/ts/$name" -L "$name" 2> "$OUT/ts/$name.err"
    pkill -P $stats 2> /dev/null || true
    { kill $stats; wait $stats; } 2> /dev/null || true
    "$CTL" stop
    rm -rf "$DBDIR/uuidbench"
    if [ ! -f "$OUT/runs.csv" ]; then
        echo "name,key,variant,cache_mb,rep,table_rows,$(head -1 "$OUT/ts/$name.summary.csv")" > "$OUT/runs.csv"
    fi
    echo "$name,$key,$variant,$size,$rep,$(rows_for "$size"),$(tail -1 "$OUT/ts/$name.summary.csv")" >> "$OUT/runs.csv"
}

measure() {
    for size in $SIZES; do
        case $size in
        16 | 64) reps=3 runs="v4:base v4:cap32 v4:unlocked v4:both v7:base" ;;
        256) reps=2 runs="v4:base v4:cap32 v4:unlocked v7:base" ;;
        *) reps=1 runs="v4:base v7:base v4:cap32 v4:unlocked" ;;
        esac
        for rep in $(seq $reps); do
            # The 1GB runs keep the baselines first so they finish even if the
            # night runs out.
            [ "$size" -ge 1024 ] || runs=$(shuf -e $runs)
            for r in $runs; do
                measure_one "${r%%:*}" "${r##*:}" "$size" "$rep"
            done
        done
    done
}

case $PHASE in
preload) preload v4; preload v7 ;;
measure) measure ;;
all) preload v4; preload v7; measure ;;
*) echo "usage: $0 outdir [preload|measure|all]" >&2; exit 2 ;;
esac
