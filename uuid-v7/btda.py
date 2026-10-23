"""Summarize a run_btda.sh results directory.

    python3 btda.py <results dir> [steady-state start, seconds into each run]

For each workload: client throughput and latency, the server's buffer-pool
counters per 1000 operations over the steady-state part of the run, and where
the server's threads were in the stack samples.
"""
import csv, math, os, statistics, sys
from collections import Counter, defaultdict


def stat_samples(path):
    out, cur, in_log = [], None, False
    for line in open(path):
        line = line.strip()
        if line.startswith("== "):
            cur = {"t": float(line[3:])}
            out.append(cur)
            in_log = False
            continue
        if ":" not in line or cur is None:
            continue
        k, v = line.split(":", 1)
        # bdb logstat follows bdb cachestat and reuses some names.
        if k == "st_magic":
            in_log = True
        if in_log:
            k = "log_" + k
        try:
            cur[k] = float(v)
        except ValueError:
            pass
    return [s for s in out if "st_alloc" in s and "log_st_scount" in s]


def hist(path):
    h = defaultdict(int)
    for r in csv.DictReader(open(path)):
        h[float(r["latency_us"])] += int(r["count"])
    return h


def pct(h, p):
    n = sum(h.values())
    want, seen = math.ceil(p * n), 0
    for x in sorted(h):
        seen += h[x]
        if seen >= want:
            return x


def ops_in_window(ts_path, t0):
    rows = list(csv.DictReader(open(ts_path)))
    w = [r for r in rows if float(r["t_s"]) >= t0]
    if len(w) < 2:
        return 0, 0
    return int(w[-1]["rows"]) - int(w[0]["rows"]), float(w[-1]["t_s"]) - float(w[0]["t_s"])


def stacks(path):
    """Yield each thread's frames, innermost first, from eu-stack samples."""
    cur = None
    for line in open(path):
        if line.startswith("TID "):
            if cur:
                yield cur
            cur = []
        elif line.startswith("=== sample") or line.startswith("PID"):
            if cur:
                yield cur
            cur = None
        elif line.startswith("#") and cur is not None:
            cur.append(line.split()[-1])
    if cur:
        yield cur


def where(frames):
    s = set(frames)
    if "__memp_alloc_flags" in s:
        if "__memp_pgwrite_multi" in s or "__memp_bhwrite" in s:
            return "alloc: writing a dirty page"
        top = frames[0]
        if "mutex" in top or "lll_lock" in top or "futex" in top:
            return "alloc: waiting on or taking a lock"
        return "alloc: other (scanning)"
    if "__memp_fget_internal" in s:
        if any(f in s for f in ("pread64", "__pread64", "__memp_pgread", "__os_io")):
            return "fget: reading a page"
        return "fget: other"
    if "__memp_fput" in s or "__memp_fput_internal" in s:
        return "fput"
    if any(f in s for f in ("__lock_get_internal_int", "__lock_get", "__lock_vec")):
        return "row/page lock"
    if "__log_put" in s or "__log_put_int" in s or "__log_flush" in s:
        return "log write/flush"
    if any(f in s for f in ("trans_commit_adaptive", "__txn_commit", "bdb_tran_commit_with_seqnum_int")):
        return "commit"
    if frames[0].startswith("pthread_cond") or "poll" in frames[0] or "read" == frames[0]:
        return "waiting (cond/poll)"
    return "other"


def profile(path):
    groups = {"apply": Counter(), "sql": Counter()}
    for f in stacks(path):
        s = set(f)
        if "toblock_main_int" in s:
            groups["apply"][where(f)] += 1
        elif "sqlengine_work_appsock" in s:
            groups["sql"][where(f)] += 1
    return groups


KEYS = ["st_page_in", "st_rw_evict", "st_ro_evict", "st_page_trickle", "st_alloc", "st_alloc_buckets",
        "st_region_wait", "st_hash_wait"]


def main():
    d = sys.argv[1]
    steady = float(sys.argv[2]) if len(sys.argv) > 2 else 300
    ts = os.path.join(d, "ts")
    labels = sorted({f.split(".")[0] for f in os.listdir(ts) if f.endswith(".stat.txt")})
    for w in labels:
        base = os.path.join(ts, w)
        parts = [(w, base)] if os.path.exists(base + ".summary.csv") else \
                [(w + "." + p, base + "." + p) for p in ("read", "insert")]
        ss = stat_samples(base + ".stat.txt")
        t_start = ss[0]["t"]
        win = [s for s in ss if s["t"] - t_start >= steady]
        print(f"## {w}")
        total_ops = 0
        for name, b in parts:
            sm = next(csv.DictReader(open(b + ".summary.csv")))
            h = hist(b + ".hist.csv")
            n = sum(h.values())
            ops, secs = ops_in_window(b + ".ts.csv", steady)
            total_ops += ops
            slow = sum(c for x, c in h.items() if x >= 4000) / n * 100
            print(f"  {name}: {float(sm['rows_per_s']):.0f} rows/s overall, {ops / secs:.0f} in steady state; "
                  f"p50={pct(h, .5) / 1000:.2f} p99={pct(h, .99) / 1000:.2f} p99.9={pct(h, .999) / 1000:.2f} ms; "
                  f">=4ms {slow:.2f}%")
        if len(win) < 2 or total_ops == 0:
            continue
        tot = {k: win[-1][k] - win[0][k] for k in KEYS}
        per_k = " ".join(f"{k[3:]}={tot[k] / total_ops * 1000:.1f}" for k in KEYS)
        dirty = statistics.median(s["st_page_dirty"] / s["st_pages"] for s in win)
        bpa = tot["st_alloc_buckets"] / tot["st_alloc"] if tot["st_alloc"] else 0
        print(f"  per 1000 ops (steady): {per_k}")
        print(f"  buckets per alloc {bpa:.1f}; max buckets in one alloc {ss[-1]['st_alloc_max_buckets']:.0f} "
              f"of {ss[-1]['st_hash_buckets'] / ss[-1]['st_ncache']:.0f} per region; "
              f"median dirty share {dirty:.0%}")
        for g, c in profile(base + ".eustack.txt").items():
            n = sum(c.values())
            if n:
                print(f"  stacks, {g} threads ({n} samples): " +
                      ", ".join(f"{k} {v / n:.0%}" for k, v in c.most_common()))


if __name__ == "__main__":
    main()
