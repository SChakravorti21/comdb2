"""Summarize a run_sweep.sh results directory.

    python3 sweep.py <results dir>

One line per run, then the median over repetitions of each cache size, key and
allocator variant. Server counters are per 1000 rows over the whole run.
"""
import csv, os, statistics, sys
from collections import defaultdict

from btda import hist, pct, stat_samples

COLS = ["rows/s", "p50", "p99", "p99.9", ">=4ms%", "stall_s", "rw_evict", "ro_evict", "skip_dirty",
        "bkt/alloc", "max_bkt", "dirty%", "region_wait"]


def run_stats(d, r):
    base = os.path.join(d, "ts", r["name"])
    h = hist(base + ".hist.csv")
    n = sum(h.values())
    rows = int(r["rows"])
    ss = stat_samples(base + ".stat.txt")
    first, last = ss[0], ss[-1]
    delta = lambda k: last[k] - first[k]
    tsrows = list(csv.DictReader(open(base + ".ts.csv")))
    # The last interval is cut short when the run ends.
    stall = sum(1 for t in tsrows[:-1] if float(t["rows_per_s"]) < 2000)
    return {
        "rows/s": float(r["rows_per_s"]),
        "p50": pct(h, .5) / 1000,
        "p99": pct(h, .99) / 1000,
        "p99.9": pct(h, .999) / 1000,
        ">=4ms%": sum(c for x, c in h.items() if x >= 4000) / n * 100,
        "stall_s": stall,
        "rw_evict": delta("st_rw_evict") / rows * 1000,
        "ro_evict": delta("st_ro_evict") / rows * 1000,
        "skip_dirty": delta("st_rw_evict_skip") / rows * 1000,
        "bkt/alloc": delta("st_alloc_buckets") / max(delta("st_alloc"), 1),
        "max_bkt": last["st_alloc_max_buckets"],
        "dirty%": statistics.median(s["st_page_dirty"] / s["st_pages"] * 100 for s in ss if s.get("st_pages")),
        "region_wait": delta("st_region_wait") / rows * 1000,
    }


def fmt(v):
    return f"{v:9.0f}" if abs(v) >= 1000 else f"{v:9.2f}"


def main():
    d = sys.argv[1]
    groups = defaultdict(list)
    print(f"{'run':32}" + "".join(f"{c:>9}" for c in COLS))
    for r in csv.DictReader(open(os.path.join(d, "runs.csv"))):
        s = run_stats(d, r)
        groups[(int(r["cache_mb"]), r["key"], r["variant"])].append(s)
        print(f"{r['name']:32}" + "".join(fmt(s[c]) for c in COLS))
    print("\nmedian over repetitions (count in brackets); latency in ms")
    print(f"{'cache MB key variant':32}" + "".join(f"{c:>9}" for c in COLS))
    for k in sorted(groups):
        g = groups[k]
        label = f"{k[0]:5} {k[1]} {k[2]} [{len(g)}]"
        print(f"{label:32}" + "".join(fmt(statistics.median(s[c] for s in g)) for c in COLS))


if __name__ == "__main__":
    main()
