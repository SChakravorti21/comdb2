"""Summarize a run_prod.sh results directory.

    python3 prod.py <results dir> [rep ...]

One line per run, then the median over repetitions of each size, key and
client count. Give repetition numbers to use only those. Server counters are per
1000 rows over the measured part of the run.
"""
import csv, os, statistics, sys
from collections import defaultdict

from btda import hist, pct, stat_samples

COLS = ["rows/s", "p50", "p99", "p99.9", ">=4ms%", "slow_s", "ts_p99", "miss", "lockwait", "deadlk",
        "rw_evict", "bkt/alloc", "dirty%", "load"]
KEYS = ["int_seq", "int_rand", "v7", "v4", "nextseq", "nextseq_v7", "nextseq_v4"]


def slow_start(tsrows):
    """Leading seconds below 70% of the run's median per-second throughput."""
    rates = [float(t["rows_per_s"]) for t in tsrows[:-1]]
    if not rates:
        return 0
    floor = 0.7 * statistics.median(rates)
    n = 0
    for x in rates:
        if x >= floor:
            break
        n += 1
    return n


def run_stats(d, r):
    base = os.path.join(d, "ts", r["name"])
    h = hist(base + ".hist.csv")
    n = sum(h.values())
    rows = int(r["rows"])
    ss = stat_samples(base + ".stat.txt")
    first, last = ss[0], ss[-1]
    delta = lambda k: last[k] - first[k]
    # The last interval is cut short when the run ends.
    tsrows = list(csv.DictReader(open(base + ".ts.csv")))
    return {
        "rows/s": float(r["rows_per_s"]),
        "p50": pct(h, .5) / 1000,
        "p99": pct(h, .99) / 1000,
        "p99.9": pct(h, .999) / 1000,
        ">=4ms%": sum(c for x, c in h.items() if x >= 4000) / n * 100,
        "slow_s": slow_start(tsrows),
        "ts_p99": statistics.median(float(t["lat_p99_us"]) for t in tsrows[:-1]) / 1000,
        "miss": int(r["cache_misses"]) / rows * 1000,
        "lockwait": int(r["lockwait_time"]) / rows,
        "deadlk": int(r["deadlocks"]) / rows * 1000,
        "rw_evict": delta("st_rw_evict") / rows * 1000,
        "bkt/alloc": delta("st_alloc_buckets") / max(delta("st_alloc"), 1),
        "dirty%": statistics.median(s["st_page_dirty"] / s["st_pages"] * 100 for s in ss if s.get("st_pages")),
        "load": float(r["loadavg_before"]),
    }


def fmt(v):
    return f"{v:9.0f}" if abs(v) >= 1000 else f"{v:9.2f}"


def main():
    d = sys.argv[1]
    reps = {int(x) for x in sys.argv[2:]}
    groups = defaultdict(list)
    print(f"{'run':32}" + "".join(f"{c:>9}" for c in COLS))
    for r in csv.DictReader(open(os.path.join(d, "runs.csv"))):
        if reps and int(r["rep"]) not in reps:
            continue
        s = run_stats(d, r)
        groups[(r["size"], r["key"], int(r["threads"]))].append(s)
        print(f"{r['name']:32}" + "".join(fmt(s[c]) for c in COLS))
    print("\nmedian over repetitions (count in brackets); latency in ms")
    print(f"{'size key clients':32}" + "".join(f"{c:>9}" for c in COLS))
    for k in sorted(groups, key=lambda k: (k[0] != "fit", KEYS.index(k[1]), k[2])):
        g = groups[k]
        label = f"{k[0]} {k[1]} {k[2]} [{len(g)}]"
        print(f"{label:32}" + "".join(fmt(statistics.median(s[c] for s in g)) for c in COLS))


if __name__ == "__main__":
    main()
