#!/bin/bash
# Record the experimental setup of the benchmark host and running database.
BASE=${BASE:-/bb/comdb2a/$USER/uuid-v7}
CTL=$(cd "$(dirname "$0")" && pwd)/ctl.sh

echo "== date";       date -Is
echo "== host";       hostname; uname -r; cat /etc/redhat-release
echo "== cpu";        lscpu | grep -E '^(Model name|CPU\(s\)|Thread|Core|Socket|NUMA node\(s\))'
echo "== memory";     free -g | head -2
echo "== disk";       df -h "$BASE"; lsblk -d -o NAME,ROTA,MODEL | grep -v '^sda'
echo "== loadavg";    cat /proc/loadavg
echo "== comdb2 sha"; cat "$BASE/src/SHA"
echo "== build";      grep -E '^CMAKE_BUILD_TYPE|^CMAKE_C_COMPILER:' "$BASE/build/CMakeCache.txt"; cc --version | head -1
echo "== lrl";        cat "$BASE/db/uuidbench/uuidbench.lrl"
echo "== tunables"
"$CTL" sql "select name, value from comdb2_tunables where name in ('cachekb', 'directio', 'maxwt', 'init_with_rowlocks', 'init_with_rowlocks_master_only', 'maxretries', 'penaltyincpercent', 'disable_writer_penalty_deadlock', 'pagesize', 'logfilesize', 'checkpointtime')"
