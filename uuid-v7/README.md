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

1. **With one row per transaction, the right-edge contention is real and large.**
   - From 16 clients up, v4 inserts are 2.3–2.6x faster than v7 (38k vs 17k
     rows/s at 16 clients; 35k vs 14k at 64).
   - v7 latency is 2.5x higher at 64 clients: p50 4.4 ms vs 1.7 ms, and p99
     8.7 ms vs 3.2 ms.
   - With in-order keys, every insert modifies the same last leaf page, and
     with page locks it holds that lock until commit. So concurrent inserts
     queue behind each other, and some deadlock.
   - v7 had 61–66 deadlocks per 1000 rows and 190–240 µs of lock wait per
     row. v4 had 0.04 deadlocks per 1000 rows and 1 µs of lock wait, because
     its inserts are spread across thousands of leaves.
   - int append behaves the same way as v7: the same lock wait, with half the
     deadlocks. It is only 1.2–1.3x faster than v7.

2. **At 1–4 clients there is little difference, and v7 is slightly ahead.**
   - With 1 client, v7 is 14% faster than v4 (6.6k vs 5.8k rows/s) and has a
     lower p99 (0.23 ms vs 0.37 ms).
   - With 4 clients, the two are within 10% of each other.
   - In-order keys stop scaling at 4 clients. Random keys keep scaling to 16.

3. **v4 wins despite missing the cache.** v4 had 55–57 buffer-pool misses per
   1000 rows; v7 and the append keys had none. Here, a page read from NVMe
   costs much less than waiting for the lock on the last page. This table is
   only about 1.5x the cache size, though. With much larger tables, v4's misses
   would grow and the balance could shift (not tested).

4. **`nextsequence` is the slowest choice beyond 1 client.**
   - With 1 client it matches int append (6.1k vs 6.5k rows/s).
   - From 4 clients up it is 1.5–2.1x slower than a client-side counter
     (9.7k vs 20.5k rows/s at 16 clients), and it peaks at 4 clients.
   - From 16 clients up, nearly every second insert deadlocks (about 450 per
     1000 rows).
   - Each insert also writes about 780 more bytes of log per row than int
     append.
   - The cause is in the code: each insert reads, deletes and re-adds the
     table's sequence counter in llmeta inside the insert's own transaction
     (`bdb_increment_and_set_sequence`, `bdb/llmeta.c:4044`). So every
     inserter into the table serializes on that one record, whatever the key
     type.

5. **For the sequence + UUID tables, the sequence is the bottleneck.**
   - The v7 index is about 10% faster than the v4 index (8.2–10.8k vs
     7.5–9.7k rows/s), with a lower p99 (11.8 ms vs 13.8 ms at 64 clients).
   - The v4 index adds about 58 cache misses per 1000 rows.
   - The order of the second column hardly matters, because every insert is
     already waiting on the sequence counter.

**Index size.** With 1 client, v7's PK index is smaller than v4's (44 vs 49
bytes per row). With 4 or more clients it is larger (52–56). The UUID index on
the sequence tables shows the same pattern.

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
- 1, 4, 16 or 64 concurrent clients.
- Each run inserts 500k rows into a freshly created table.
- 3 repetitions of each of the 28 workload/client combinations: 84 runs.
- Within each repetition, the run order is shuffled so that drift on the host
  does not line up with one workload.

**Cache.** comdb2's cache is set to 16MB, so the v4 PK index (about 25MB at
500k rows) outgrows it. comdb2 raises any cache below `cachekbmin` up to it
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
| host | c2btda-pw-928, the least loaded C2BTDA host when this work started |
| CPU | 2x Xeon Gold 6240, 72 logical CPUs, 2 NUMA nodes |
| RAM | 1.1 TiB |
| disk | XFS on LVM over Intel P4510 NVMe (SSDPE2KX080T8) |
| OS | RHEL 8.10, kernel 4.18.0-553 |
| comdb2 | `a6948d3ff` (branch `uuid-v7`), RelWithDebInfo, gcc 8.5 |
| topology | one node; clients run on the same host |
| locking | page locks (`init_with_rowlocks 0`) |
| writer threads | `maxwt 8` (default): at most 8 transactions are applied on the master at once |
| other settings | defaults: `maxretries 500`, `penaltyincpercent 20`, `checkpointtime 60`, `logfilesize 40MB` |
| run time | 2026-10-14 16:28–17:39 EDT; 68 minutes of measured insert time |

The database registers with a private pmux on port 15105. The host's shared
pmux handed out a port that another process was already using for UDP.

## Results

Each point is the median of 3 repetitions. Whiskers show min..max. The median
spread between repetitions is 8% of the median; the worst case is 25% (v7 at 64
clients). Full tables for every metric are in
[`results/summary.md`](results/summary.md).

### Throughput

![throughput](results/plots/throughput.png)

| workload | 1 client | 4 clients | 16 clients | 64 clients |
|---|---:|---:|---:|---:|
| uuid v7 PK | 6,596 | 19,598 | 16,768 | 13,564 |
| uuid v4 PK | 5,808 | 17,848 | 38,209 | 34,962 |
| int append PK | 6,512 | 19,568 | 20,545 | 17,192 |
| int random PK | 5,780 | 19,082 | 40,947 | 35,059 |
| nextsequence PK | 6,056 | 12,725 | 9,711 | 9,152 |
| nextsequence PK + v7 index | 5,722 | 10,844 | 8,239 | 8,366 |
| nextsequence PK + v4 index | 5,156 | 9,657 | 7,465 | 7,508 |

### Latency

![latency](results/plots/latency.png)
![latency distribution](results/plots/latency_cdf.png)

The second chart shows, for every insert in all three repetitions, the
fraction that took longer than a given latency. The dotted lines mark p50, p99
and p99.9.

- **1 client:** all workloads are close, and the in-order keys have slightly
  shorter tails.
- **16 and 64 clients:** v4 and int random sit far to the left at every
  percentile.
- **The tail below p99.9:** the in-order and sequence workloads have a long
  tail of slow inserts that reaches hundreds of milliseconds. These are inserts
  that deadlocked and were retried after comdb2's backoff sleep.

p99 latency, ms:

| workload | 1 client | 4 clients | 16 clients | 64 clients |
|---|---:|---:|---:|---:|
| uuid v7 PK | 0.23 | 0.34 | 3.20 | 8.70 |
| uuid v4 PK | 0.37 | 0.54 | 0.80 | 3.20 |
| int append PK | 0.22 | 0.30 | 2.69 | 7.42 |
| int random PK | 0.34 | 0.43 | 0.74 | 3.20 |
| nextsequence PK | 0.23 | 1.73 | 3.97 | 10.75 |
| nextsequence PK + v7 index | 0.27 | 2.18 | 4.86 | 11.78 |
| nextsequence PK + v4 index | 0.40 | 2.69 | 5.38 | 13.82 |

![p99 over time](results/plots/timeline_p99.png)

### Contention

![lock wait](results/plots/lockwait.png)
![aborted locks](results/plots/locks_aborted.png)

The two kinds of contention look different:

- **In-order keys (v7, int append)** mostly *wait*: 190–240 µs of lock wait per
  row from 16 clients up.
- **The `nextsequence` tables** wait less (70–95 µs per row) but abort far more
  often: about 900 aborted lock requests and 450 deadlocks per 1000 rows. Each
  deadlock sends the transaction into comdb2's retry path, which sleeps for a
  random interval and lowers the effective number of write threads
  (`handle_op_local`, `db/sltdbt.c`).

### Throughput and cache misses as the table grows

![throughput over time](results/plots/timeline_throughput.png)
![cache misses over time](results/plots/timeline_cache_misses.png)

v4 misses the cache from the first second of every run, and the misses grow as
the table grows. Its throughput at 16 clients drifts down from about 44k to
32–35k rows/s, but it stays far ahead of v7 for the whole run.

### Space

![sizes](results/plots/sizes.png)

The `nextsequence` tables write about 1,830 bytes of log per row (2,170 with
the UUID index), against about 1,050–1,130 for the client-generated keys. The
difference is the delete and add of the llmeta sequence record in every insert.

## Caveats

- **Table size.** Tables reach 500k rows, which is only about 1.5x the 16MB
  cache. At sizes many times the cache, v4 would miss on most inserts; whether
  that erases its lead was not tested.
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
  each run ranged from 2.3 to 14.8 (median 7.1); it includes the benchmark's own
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
$BASE/bench-src/scripts/run_all.sh $BASE/results          # about 75 minutes
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
