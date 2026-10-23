#!/bin/bash
# The original matrix (7 workloads, 4/8/16/64 clients, 3 repetitions) with the
# production default cache: 128MB, from "cachekbmin 131072" in the DBAs' prod
# comdb2_local.lrl. Two table sizes: "fit", whose indexes fit in the cache,
# and "big", whose PK index ends up about 12x the cache.
#
# Preload grows one table per workload in 100-row transactions and snapshots it
# at each size. Each measured run starts a server on a copy of the snapshot,
# warms the cache with unmeasured single-row inserts (a fresh server starts
# with an empty cache, and the sweep showed v4 running slowly for up to 28 s
# after that), then measures single-row inserts while sampling the server's
# cache and log stats every second.
#
#   run_prod.sh <outdir> [preload|measure|all]
set -e

OUT=$1
PHASE=${2:-all}
HERE=$(cd "$(dirname "$0")" && pwd)
CTL=$HERE/ctl.sh
BENCH=$HERE/../bench/uuidbench
BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
export BUILD=${BUILD:-$BASE/build}
KEYS=${KEYS:-int_seq int_rand v7 v4 nextseq nextseq_v7 nextseq_v4}
THREADS=${THREADS:-4 8 16 64}
REPS=${REPS:-3}
MEASURE_CACHE_MB=128
PRELOAD_CACHE_MB=16384
# rows: preloaded, warm-up, measured. fit's v4 PK index is about 80MB after
# its run; big's is about 1.6GB.
FIT_ROWS=${FIT_ROWS:-1000000} FIT_WARM=${FIT_WARM:-100000} FIT_MEASURED=${FIT_MEASURED:-500000}
BIG_ROWS=${BIG_ROWS:-32000000} BIG_WARM=${BIG_WARM:-300000} BIG_MEASURED=${BIG_MEASURED:-1000000}
mkdir -p "$OUT/ts"

rows_of() { [ "$1" = fit ] && echo "$FIT_ROWS $FIT_WARM $FIT_MEASURED" || echo "$BIG_ROWS $BIG_WARM $BIG_MEASURED"; }

# The cache size is fixed in the lrl at create time; rewrite it per phase.
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
    export DBDIR=$BASE/db-prod-$key CACHE_MB=$PRELOAD_CACHE_MB
    "$CTL" stop
    "$CTL" create
    "$CTL" start
    "$CTL" mktable "$key" pk
    for size in fit big; do
        local want
        want=$(rows_of $size | cut -d' ' -f1)
        "$BENCH" -d uuidbench -c "$DBDIR/comdb2db.cfg" -k "$key" -n $((want - have)) -s $have -T 16 -b 100 \
            -o "$OUT/ts/preload_${key}_$size" -L "preload_${key}_$size" 2> "$OUT/ts/preload_${key}_$size.err"
        have=$want
        "$CTL" sql "exec procedure sys.cmd.send('flush')" > /dev/null
        "$CTL" stop
        rm -rf "$DBDIR/snap_$size"
        cp -a "$DBDIR/uuidbench" "$DBDIR/snap_$size"
        (cd "$DBDIR/snap_$size" && ls -l t_*) > "$OUT/snap_${key}_$size.txt"
        "$CTL" start
    done
    "$CTL" stop
    rm -rf "$DBDIR/uuidbench"
}

measure_one() {
    local key=$1 size=$2 T=$3 rep=$4 seq name rows warm measured load log0 log1
    export DBDIR=$BASE/db-prod-$key
    read -r rows warm measured <<< "$(rows_of "$size")"
    seq=$(($(ls "$OUT"/ts/ | grep -c '\.warm\.summary\.csv$' || true) + 1))
    name=$(printf "%03d_%s_%s_T%s_r%s" "$seq" "$key" "$size" "$T" "$rep")
    rm -rf "$DBDIR/uuidbench"
    cp -a "$DBDIR/snap_$size" "$DBDIR/uuidbench"
    set_cache $MEASURE_CACHE_MB
    "$CTL" start
    [ "$seq" = 1 ] && "$HERE/env.sh" >> "$OUT/env.txt" 2>&1
    "$BENCH" -d uuidbench -c "$DBDIR/comdb2db.cfg" -k "$key" -n "$warm" -s "$rows" -T "$T" -b 1 \
        -o "$OUT/ts/$name.warm" -L "$name.warm" 2> "$OUT/ts/$name.warm.err"
    load=$(cut -d' ' -f1 /proc/loadavg)
    log0=$("$CTL" logbytes)
    sample_stats > "$OUT/ts/$name.stat.txt" &
    local stats=$!
    "$BENCH" -d uuidbench -c "$DBDIR/comdb2db.cfg" -k "$key" -n "$measured" -s $((rows + warm)) -T "$T" -b 1 \
        -o "$OUT/ts/$name" -L "$name" 2> "$OUT/ts/$name.err"
    pkill -P $stats 2> /dev/null || true
    { kill $stats; wait $stats; } 2> /dev/null || true
    log1=$("$CTL" logbytes)
    "$CTL" stop
    rm -rf "$DBDIR/uuidbench"
    if [ ! -f "$OUT/runs.csv" ]; then
        echo "name,key,size,rep,table_rows,loadavg_before,log_bytes,$(head -1 "$OUT/ts/$name.summary.csv")" > "$OUT/runs.csv"
    fi
    echo "$name,$key,$size,$rep,$((rows + warm)),$load,$((log1 - log0)),$(tail -1 "$OUT/ts/$name.summary.csv")" >> "$OUT/runs.csv"
}

# Each repetition covers both sizes, so a night cut short still leaves whole
# repetitions; the order within one is shuffled.
measure() {
    for rep in $(seq "$REPS"); do
        for size in $(shuf -e fit big); do
            for T in $(shuf -e $THREADS); do
                for key in $(shuf -e $KEYS); do
                    measure_one "$key" "$size" "$T" "$rep"
                done
            done
        done
    done
}

case $PHASE in
preload) for key in $KEYS; do preload "$key"; done ;;
measure) measure ;;
all) for key in $KEYS; do preload "$key"; done; measure ;;
*) echo "usage: $0 outdir [preload|measure|all]" >&2; exit 2 ;;
esac
