# Insert benchmark: UUID v7 vs UUID v4 vs integer keys in comdb2

This benchmark measures how the order of primary-key values affects
single-row insert performance in comdb2. It compares:

- time-ordered UUIDs (v7) against random UUIDs (v4);
- both against integer keys inserted in order (append) and at random;
- three tables whose id is assigned by the server with `nextsequence`: the
  sequence alone, and the sequence plus a uniquely indexed v7 or v4 UUID
  column.

It also tests one specific concern: that v7 keys, which all land at the
right-most leaf page of the index, make concurrent writers contend on that
page.

## Findings

The tables grow to 4M rows against a 16MB cache, so by the end of a run the
random-key indexes are 9–12x the cache (v4 PK index about 195MB, int random
about 148MB). "Last 1M rows" below means throughput measured from row 3M to
row 4M, once v4 misses the cache on about 906 of every 1000 inserts.

1. **v4 still beats v7 from 16 clients up, but by much less.**
   - Over the last 1M rows, v4 is 1.16–1.18x faster than v7 (14.7k vs 12.6k
     rows/s at 16 clients; 15.4k vs 13.1k at 64). Over the whole run it is
     1.38–1.42x faster. In the earlier 500k-row run, where the v4 index was
     only about 1.5x the cache, it was 2.3–2.6x faster.
   - v4 slows down as its index outgrows the cache: at 16 clients it runs at
     34–36k rows/s over the first 500k rows and 13.7–15.9k over the last 1M.
     v7 holds steady at 12.6–13.1k.
   - v4 stays ahead because v7's problem is still there. Every v7 insert
     modifies the same last leaf page, and with page locks it holds that lock
     until commit, so concurrent inserts queue and some deadlock. From 16
     clients up, v7 has 230–233 µs of lock wait and 58–60 deadlocks per 1000
     rows; v4 has under 1 µs and 0.01. A page read from NVMe still costs less
     than that queue.
   - int append behaves like v7 (255–276 µs of lock wait per row) and is within
     12% of it.

2. **At 4 clients, v7 is now well ahead.** v7 is 1.7x faster than v4 over the
   whole run (16.0k vs 9.5k rows/s) and 2.0x faster over the last 1M rows
   (16.1k vs 8.3k). With few writers there is little lock contention, so each
   v4 cache miss shows up directly in the throughput. In the 500k-row run the
   two were within 10% at 4 clients.

3. **v4's throughput lead comes with a worse tail.** v4's p99 is higher than
   v7's at every client count: 1.86 vs 0.43 ms at 4 clients, 10.75 vs 4.35 ms
   at 16, and 38.9 vs 9.7 ms at 64. Its p50 is still lower from 16 clients up
   (2.43 vs 4.35 ms at 64). The slow v4 inserts are the ones that wait for a
   page read.

4. **`nextsequence` is the slowest choice.**
   - It is 1.6–1.9x slower than a client-side counter (10.7k vs 16.8k rows/s
     at 4 clients; 7.6k vs 14.7k at 16), and it is fastest at 4 clients.
   - From 16 clients up, nearly every second insert deadlocks (443–456 per 1000
     rows).
   - Each insert writes about 770–780 more bytes of log than int append.
   - The cause is in the code: each insert reads, deletes and re-adds the
     table's sequence counter in llmeta inside the insert's own transaction
     (`bdb_increment_and_set_sequence`, `bdb/llmeta.c:4044`). So every
     inserter into the table serializes on that one record, whatever the key
     type.

5. **For the sequence + UUID tables, the UUID order now matters a lot.** The
   v7 index is 1.7–2.2x faster than the v4 index (10.0k vs 4.5k rows/s at 4
   clients; 7.1k vs 4.0k at 16; 6.9k vs 4.1k at 64), and has a lower p99 at
   every client count. The v4 index misses the cache on about 697 of every
   1000 inserts over the run. Every insert already serializes on the sequence
   record, so a page read for the v4 index lengthens that serialized section:
   lock wait is 182 µs per row with the v4 index against 84–88 µs with the v7
   index.

**Index size.** v7's PK index is larger than v4's (52–55 vs 49 bytes per row).
The UUID index on the sequence tables shows the same pattern (53–57 vs 49).

## What was run

**Workloads.** Every workload inserts one row per transaction (a single-row
`INSERT` in autocommit) and measures writes only. Every table also has
`longlong v` (random) and `byte pad[100]` (random bytes).

| workload | key columns and indexes | who generates the key |
|---|---|---|
| `int_seq` (int append PK) | `longlong k`, PK | client: shared counter, one value per insert |
| `int_rand` (int random PK) | `longlong k`, PK | client: uniform random 64-bit value |
| `v7` (uuid v7 PK) | `byte k[16]`, PK | client: RFC 9562 v7, 48-bit Unix milliseconds then random bits (no counter within a millisecond) |
| `v4` (uuid v4 PK) | `byte k[16]`, PK | client: RFC 9562 v4 |
| `nextseq` (nextsequence PK) | `longlong id dbstore=nextsequence`, PK | server |
| `nextseq_v7` | `id` as above, PK; `byte u[16]` v7, unique index | server for id, client for u |
| `nextseq_v4` | `id` as above, PK; `byte u[16]` v4, unique index | server for id, client for u |

**Load and size.**
- 4, 16 or 64 concurrent clients. 1 client was left out: at this table size
  it would have taken longer than the rest of the matrix combined.
- Each run inserts 4M rows into a freshly created table.
- 3 repetitions of each of the 21 workload/client combinations: 63 runs.
- Within each repetition, the run order is shuffled so that drift on the host
  does not line up with one workload.

**Cache.** comdb2's cache is set to 16MB, so the random-key indexes end up
9–12x larger than the cache. comdb2 raises any cache below `cachekbmin` up to it
(64MB by default), so the lrl lowers `cachekbmin` to match. comdb2 uses direct
I/O by default (`directio ON`), so a cache miss is a real NVMe read; the host's
1.1 TiB of RAM does not act as a hidden page cache.

**Between runs.** The table is dropped and recreated, and the cache is flushed
(`sys.cmd.send('flush')`).

**What each run records.**
- `results/main/runs.csv`, one row per run:
  - throughput and commit-latency percentiles
  - deltas of server counters from `comdb2_metrics`: lock waits, lock-wait
    time, aborted lock requests, deadlocks, cache hits and misses
  - bytes written to the transaction log (`bdb logstat`)
  - on-disk size of the data and index files after a flush
- `results/main/ts/<run>.ts.csv`: the same client and server counters sampled
  every second.
- `results/main/ts/<run>.hist.csv`: the full commit-latency histogram for the
  run.

## Setup

From `results/main/env.txt`:

| | |
|---|---|
| host | c2btda-pw-188, one of the least loaded C2BTDA hosts when the run started |
| CPU | 2x Xeon Gold 6240, 72 logical CPUs, 2 NUMA nodes |
| RAM | 1.1 TiB |
| disk | XFS on LVM over Intel P4510 NVMe (SSDPE2KX080T8) |
| OS | RHEL 8.10, kernel 4.18.0-553 |
| comdb2 | `a6948d3ff` (branch `uuid-v7`), RelWithDebInfo, gcc 8.5 |
| topology | one node; clients run on the same host |
| locking | page locks (`init_with_rowlocks 0`) |
| writer threads | `maxwt 8` (default): at most 8 transactions are applied on the master at once |
| other settings | defaults: `maxretries 500`, `penaltyincpercent 20`, `checkpointtime 60`, `logfilesize 40MB` |
| run time | 2026-10-14 19:08 to 2026-10-15 02:55 EDT; 461 minutes of measured insert time |

The database registers with a private pmux on port 15105. The host's shared
pmux handed out a port that another process was already using for UDP.

## Results

Each point is the median of 3 repetitions. Whiskers show min..max. The median
spread between repetitions is 8% of the median; the worst case is 21%
(`nextsequence` at 4 clients). Full tables for every metric are in
[`results/summary.md`](results/summary.md).

### Throughput

![throughput](results/plots/throughput.png)

Whole run, rows/s:

| workload | 4 clients | 16 clients | 64 clients |
|---|---:|---:|---:|
| uuid v7 PK | 15,993 | 13,084 | 13,026 |
| uuid v4 PK | 9,466 | 18,051 | 18,459 |
| int append PK | 16,809 | 14,715 | 13,233 |
| int random PK | 9,442 | 18,868 | 19,401 |
| nextsequence PK | 10,705 | 7,600 | 7,805 |
| nextsequence PK + v7 index | 9,972 | 7,103 | 6,855 |
| nextsequence PK + v4 index | 4,499 | 3,981 | 4,050 |

Last 1M rows (row 3M to row 4M), rows/s:

| workload | 4 clients | 16 clients | 64 clients |
|---|---:|---:|---:|
| uuid v7 PK | 16,117 | 12,628 | 13,093 |
| uuid v4 PK | 8,254 | 14,700 | 15,440 |
| int append PK | 15,777 | 14,609 | 14,062 |
| int random PK | 7,743 | 14,760 | 15,312 |
| nextsequence PK | 10,994 | 7,423 | 7,746 |
| nextsequence PK + v7 index | 9,625 | 7,205 | 6,685 |
| nextsequence PK + v4 index | 3,977 | 3,472 | 3,656 |

### Latency

![latency](results/plots/latency.png)
![latency distribution](results/plots/latency_cdf.png)

The second chart shows, for every insert in all three repetitions, the
fraction that took longer than a given latency. The dotted lines mark p50, p99
and p99.9.

- **4 clients:** v7 and int append are fastest at every percentile through
  p99.99. The random keys have a shoulder of slow inserts from about p95, which
  are the inserts that waited for a page read.
- **16 and 64 clients:** v4 and int random are fastest up to p95, cross over
  between p95 and p98, and are the slowest of all workloads at p99 and p99.9.
- **The tail beyond p99.9:** at 16 and 64 clients every workload reaches
  hundreds of milliseconds. For the in-order and sequence workloads these are
  mostly inserts that deadlocked and were retried after comdb2's backoff sleep.
  v4 and int random almost never deadlock but still reach 220–240 ms at p99.99
  with 64 clients; that tail was not investigated.

p99 latency, ms:

| workload | 4 clients | 16 clients | 64 clients |
|---|---:|---:|---:|
| uuid v7 PK | 0.43 | 4.35 | 9.73 |
| uuid v4 PK | 1.86 | 10.75 | 38.91 |
| int append PK | 0.37 | 3.71 | 8.70 |
| int random PK | 1.98 | 10.75 | 38.91 |
| nextsequence PK | 2.18 | 5.38 | 12.80 |
| nextsequence PK + v7 index | 2.43 | 5.38 | 14.85 |
| nextsequence PK + v4 index | 6.40 | 9.73 | 23.55 |

![p99 over time](results/plots/timeline_p99.png)

### Contention

![lock wait](results/plots/lockwait.png)
![aborted locks](results/plots/locks_aborted.png)

The two kinds of contention look different:

- **In-order keys (v7, int append)** mostly *wait*: 230–276 µs of lock wait per
  row from 16 clients up.
- **The `nextsequence` tables** wait less (74–88 µs per row; 182 with the v4
  index) but abort far more often: about 890–910 aborted lock requests and
  443–456 deadlocks per 1000 rows. Each
  deadlock sends the transaction into comdb2's retry path, which sleeps for a
  random interval and lowers the effective number of write threads
  (`handle_op_local`, `db/sltdbt.c`).

### Throughput and cache misses as the table grows

![throughput over time](results/plots/timeline_throughput.png)
![cache misses over time](results/plots/timeline_cache_misses.png)

v4 starts missing the cache once the table passes 180k–320k rows (int random:
270k–430k), and the misses climb to about 906 per 1000 rows over the last 1M
rows (868 for int random). Its throughput falls with them, and at 16 and 64
clients it ends up just above v7. v7, int append and plain `nextsequence` never
miss the cache.

### Space

![sizes](results/plots/sizes.png)

The `nextsequence` tables write about 1,830 bytes of log per row (2,170–2,180
with the UUID index), against about 1,050–1,140 for the client-generated keys. The
difference is the delete and add of the llmeta sequence record in every insert.

## Caveats

- **No 1-client point.** It was dropped to fit the run into one night. The
  500k-row run in the git history has it: v7 was 14% faster than v4 there.
- **Still growing.** v4 was still slowing down at 4M rows (see the throughput
  timeline). Larger tables could narrow its lead at 16+ clients further.
- **Different host from the 500k-row run.** That run used c2btda-pw-928; this
  one used c2btda-pw-188, which has the same hardware. Compare the two runs by
  ratios, not absolute rates.
- **Batched transactions.** Inserts with many rows per transaction behave
  differently, because each transaction then touches many random pages and
  contends whatever the key order. An earlier version of this benchmark with
  100-row transactions found v7 ahead of v4 at every client count; see the git
  history of this directory.
- **Configuration.**
  - Single node only, so there is no replication.
  - Default page locks; rowlocks were deliberately not tested.
  - `maxwt 8`, so the master applies at most 8 transactions at once.
- **Shared host.** Clients and server share the host. The load average before
  each run ranged from 2.7 to 18.6 (median 7.2); it includes the benchmark's own
  threads from the previous run.
- **Fresh table, not fresh database.** The database is created once for the
  whole matrix. Each run gets a freshly created table, but the transaction log
  and the files of dropped tables carry over.
- **Not measured.** Reading back the generated id after an insert.

## Reproducing

On a host with comdb2's build dependencies, with the source at `$BASE/src` and
a CMake build at `$BASE/build` (default `BASE=/bb/comdb2a/$USER/uuid-v7`):

```sh
cmake -S $BASE/src -B $BASE/build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo && ninja -C $BASE/build
cp -r uuid-v7 $BASE/bench-src
make -C $BASE/bench-src/bench SRC=$BASE/src BUILD=$BASE/build
$BASE/bench-src/scripts/run_all.sh $BASE/results          # about 8 hours
python plot.py $BASE/results                              # needs matplotlib
```

## Files

| path | what |
|---|---|
| `bench/uuidbench.c`, `bench/Makefile` | the client: key generators, insert threads, 1-second sampler, latency histogram |
| `scripts/ctl.sh` | create, start and stop the database; recreate the table; read log bytes and file sizes |
| `scripts/run_matrix.sh` | one configuration: shuffled runs, one row per run in `runs.csv` |
| `scripts/run_all.sh` | the configuration reported above |
| `scripts/env.sh` | records the host and database setup (`env.txt`) |
| `plot.py` | generates `results/plots/*.png` and `results/summary.md` |
| `results/main/` | `runs.csv`, `env.txt`, and per-run time series and histograms in `ts/` |
