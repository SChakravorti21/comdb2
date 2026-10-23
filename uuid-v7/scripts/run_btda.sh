#!/bin/bash
# Buffer-pool benchmark at a production-like scale: a 2GB cache (two 1GB
# regions) under a table preloaded with hv4 keys until its PK index is about 5x
# the cache. Each workload then runs for $DURATION seconds on a copy of the
# preloaded database and a freshly started server, with the server's cache and
# log stats sampled every second and stacks sampled $PROF_AT seconds in.
#
#   run_btda.sh <outdir> [workload...]
# workload: insert_rand insert_append update_rand read_rand read_insert insert_rand_b100
# (default: all). The preload is skipped if $DBDIR/preloaded exists.
set -e

OUT=$1
shift
WORKLOADS=${*:-read_rand update_rand insert_append insert_rand read_insert insert_rand_b100}

HERE=$(cd "$(dirname "$0")" && pwd)
CTL=$HERE/ctl.sh
BENCH=$HERE/../bench/uuidbench
BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
DBDIR=${DBDIR:-$BASE/db-btda}
CACHE_MB=${CACHE_MB:-2048}
PRELOAD_ROWS=${PRELOAD_ROWS:-200000000}
DURATION=${DURATION:-900}
PROF_AT=${PROF_AT:-600}
T=16
CFG=$DBDIR/comdb2db.cfg
export BASE DBDIR CACHE_MB

mkdir -p "$OUT/ts"

bench() { "$BENCH" -d uuidbench -c "$CFG" "$@"; }

sample_stats() {
    while :; do
        echo "== $(date +%s.%N)"
        "$CTL" sql "exec procedure sys.cmd.send('bdb cachestat')" || true
        "$CTL" sql "exec procedure sys.cmd.send('bdb logstat')" || true
        sleep 1
    done
}

sample_stacks() {
    sleep "$PROF_AT"
    local pid
    pid=$(pgrep -u "$USER" -f "[c]omdb2 --lrl $DBDIR/uuidbench")
    for i in $(seq 60); do
        echo "=== sample $i $(date +%s.%N)"
        eu-stack -p "$pid" 2> /dev/null
        sleep 0.25
    done
}

if [ ! -d "$DBDIR/preloaded" ]; then
    "$CTL" stop
    "$CTL" create
    "$CTL" start
    "$HERE/env.sh" >> "$OUT/env.txt" 2>&1
    "$CTL" mktable hv4 pk
    bench -k hv4 -n "$PRELOAD_ROWS" -T $T -b 100 -s 1 -o "$OUT/ts/preload" -L preload \
        2> "$OUT/ts/preload.err"
    "$CTL" sql "exec procedure sys.cmd.send('flush')" > /dev/null
    "$CTL" stop
    (cd "$DBDIR/uuidbench" && ls -l t_*) > "$OUT/preload_files.txt"
    cp -a "$DBDIR/uuidbench" "$DBDIR/preloaded"
fi

# Inserting workloads start their hv4 counters at distinct values so their keys
# never collide with the preloaded keys 1..PRELOAD_ROWS.
for w in $WORKLOADS; do
    rm -rf "$DBDIR/uuidbench"
    cp -a "$DBDIR/preloaded" "$DBDIR/uuidbench"
    "$CTL" start
    sample_stats > "$OUT/ts/$w.stat.txt" 2>&1 &
    stats=$!
    sample_stacks > "$OUT/ts/$w.eustack.txt" 2>&1 &
    stacks=$!
    run=(-D "$DURATION" -T $T -L "$w")
    case $w in
    insert_rand) bench "${run[@]}" -k hv4 -s 1000000000 -b 1 -o "$OUT/ts/$w" ;;
    insert_append) bench "${run[@]}" -k top -s 1 -b 1 -o "$OUT/ts/$w" ;;
    update_rand) bench "${run[@]}" -w update -k hv4 -K "$PRELOAD_ROWS" -b 1 -o "$OUT/ts/$w" ;;
    read_rand) bench "${run[@]}" -w read -k hv4 -K "$PRELOAD_ROWS" -b 1 -o "$OUT/ts/$w" ;;
    read_insert)
        bench "${run[@]}" -w read -k hv4 -K "$PRELOAD_ROWS" -b 1 -o "$OUT/ts/$w.read" \
            2> "$OUT/ts/$w.read.err" &
        readers=$!
        bench "${run[@]}" -k hv4 -s 2000000000 -b 1 -o "$OUT/ts/$w.insert" 2> "$OUT/ts/$w.insert.err"
        wait $readers
        ;;
    insert_rand_b100) bench "${run[@]}" -k hv4 -s 3000000000 -b 100 -o "$OUT/ts/$w" ;;
    *) echo "bad workload $w" >&2; exit 2 ;;
    esac 2>> "$OUT/ts/$w.err"
    { kill $stats; wait $stats; } 2> /dev/null || true
    { kill $stacks; wait $stacks; } 2> /dev/null || true
    "$CTL" stop
done
rm -rf "$DBDIR/uuidbench"
