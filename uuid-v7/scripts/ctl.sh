#!/bin/bash
# Manage the single-node benchmark database on the benchmark host.
#   ctl.sh create|start|stop|sql <stmt>|mktable <key> <shape>|logbytes|files|filesizes <files-before>
# key: int_seq|int_rand|seq16|v7|v4    shape: pk|pk_sec
set -e

BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
BUILD=${BUILD:-$BASE/build}
DBDIR=$BASE/db
DB=uuidbench
CACHE_MB=${CACHE_MB:-64}
PMUX_PORT=15105
CFG=$DBDIR/comdb2db.cfg

sql() { "$BUILD/tools/cdb2sql/cdb2sql" -c "$CFG" -tabs "$DB" local "$@"; }

case "$1" in
create)
    mkdir -p "$DBDIR"
    rm -rf "$DBDIR/$DB"
    "$BUILD/db/comdb2" --create --dir "$DBDIR/$DB" "$DB" > "$DBDIR/create.log" 2>&1
    # The host's shared pmux hands out ports that collide with other UDP
    # listeners, so this database registers with a private pmux instead.
    cat > "$CFG" <<EOF
comdb2_config:portmuxport=$PMUX_PORT
comdb2_config:default_type=local
EOF
    cat >> "$DBDIR/$DB/$DB.lrl" <<EOF
cache $CACHE_MB mb
portmux_port $PMUX_PORT
portmux_bind_path $DBDIR/pmux.socket
EOF
    ;;
start)
    if ! pgrep -u "$USER" -f "pmux -n -f -p $PMUX_PORT" > /dev/null; then
        nohup setsid "$BUILD/tools/pmux/pmux" -n -f -p $PMUX_PORT -b "$DBDIR/pmux.socket" \
            -r 31000:31100 > "$DBDIR/pmux.log" 2>&1 < /dev/null &
        sleep 1
    fi
    nohup setsid "$BUILD/db/comdb2" --lrl "$DBDIR/$DB/$DB.lrl" "$DB" \
        > "$DBDIR/server.log" 2>&1 < /dev/null &
    for _ in $(seq 60); do
        sql "select 1" > /dev/null 2>&1 && exit 0
        sleep 1
    done
    echo "server did not come up" >&2
    exit 1
    ;;
stop)
    sql "exec procedure sys.cmd.send('exit')" > /dev/null 2>&1 || true
    for _ in $(seq 60); do
        pgrep -u "$USER" -f "comdb2 --lrl $DBDIR/$DB/$DB.lrl" > /dev/null || break
        sleep 1
    done
    pkill -u "$USER" -f "pmux -n -f -p $PMUX_PORT" || true
    ;;
sql)
    shift
    sql "$@"
    ;;
mktable)
    case "$2" in
    int_seq|int_rand) ktype="longlong k" ;;
    seq16|v7|v4) ktype="byte k[16]" ;;
    *) echo "bad key $2" >&2; exit 2 ;;
    esac
    case "$3" in
    pk) keys='"PK" = k' ;;
    pk_sec) keys='"PK" = k
        dup "SEC" = v' ;;
    *) echo "bad shape $3" >&2; exit 2 ;;
    esac
    sql "drop table if exists t" > /dev/null
    sql "create table t { schema { $ktype longlong v byte pad[150] } keys { $keys } }" > /dev/null
    ;;
logbytes)
    # Total bytes ever written to the transaction log.
    sql "exec procedure sys.cmd.send('bdb logstat')" |
        awk '/st_w_mbytes:/ {mb=$2} /st_w_bytes:/ {b=$2} END {printf "%d\n", mb*1048576+b}'
    ;;
files)
    # Names of all table files, to diff against after the next create.
    cd "$DBDIR/$DB" && ls | grep '^t_' | sort || true
    ;;
filesizes)
    # Bytes on disk of table t's files that are not listed in $2 (the output
    # of "files" taken before the table was created), as
    # "data,pk_index,sec_index". Dropped tables leave their files behind,
    # hence the diff. Index files sort in key order (PK first).
    sql "exec procedure sys.cmd.send('flush')" > /dev/null
    cd "$DBDIR/$DB"
    new=$(ls | grep '^t_' | sort | comm -13 "$2" -)
    data=$(echo "$new" | grep '\.datas' | xargs stat -c %s | awk '{s+=$1} END {print s+0}')
    idx=$(echo "$new" | grep '\.index$' | xargs stat -c %s)
    echo "$data,$(echo "$idx" | sed -n 1p),$(echo "$idx" | sed -n 2p)"
    ;;
*)
    echo "usage: $0 create|start|stop|sql <stmt>|mktable <key> <shape>|logbytes|files|filesizes <files-before>" >&2
    exit 2
    ;;
esac
