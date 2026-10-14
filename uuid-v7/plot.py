"""Turn results/<config>/runs.csv and ts/*.ts.csv into PNG charts and summary tables.

usage: python plot.py [results_dir]   (needs matplotlib)
"""
import csv
import os
import statistics
import sys
from collections import defaultdict

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "results")
PLOTS = os.path.join(RESULTS, "plots")

# Colour is identity; dashes mark the keys whose inserts land at random places in the index.
KEYS = ["v7", "v4", "int_seq", "int_rand", "seq16"]
COLOR = dict(zip(KEYS, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]))
DASH = {"v7": "-", "v4": "--", "int_seq": "-", "int_rand": "--", "seq16": "-"}
KEYLABEL = {"v7": "uuid v7", "v4": "uuid v4", "int_seq": "int append", "int_rand": "int random", "seq16": "16B append"}
SHAPES = ["pk", "pk_sec"]
SHAPELABEL = {"pk": "PK only", "pk_sec": "PK + secondary index"}
THREADS = [1, 4, 16, 64]
CONFIGS = {"main": "64MB cache (index outgrows cache)", "incache": "4GB cache (everything in cache)"}

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({
    "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "savefig.facecolor": "#fcfcfb",
    "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 11, "axes.titleweight": "bold", "lines.linewidth": 2,
})


def load_runs(config):
    path = os.path.join(RESULTS, config, "runs.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        runs = list(csv.DictReader(f))
    for r in runs:
        rows = float(r["rows"])
        r["threads"] = int(r["threads"])
        r["rows_per_s"] = float(r["rows_per_s"])
        r["deadlocks_per_krow"] = 1000 * float(r["deadlocks"]) / rows
        r["lockwait_us_per_row"] = float(r["lockwait_time"]) / rows
        r["cache_misses_per_krow"] = 1000 * float(r["cache_misses"]) / rows
        r["log_bytes_per_row"] = float(r["log_bytes"]) / rows
        r["data_bytes_per_row"] = float(r["data_bytes"]) / rows
        r["pk_index_bytes_per_row"] = float(r["pk_index_bytes"]) / rows
        r["sec_index_bytes_per_row"] = float(r["sec_index_bytes"] or 0) / rows
        r["lat_p99_ms"] = float(r["lat_p99_us"]) / 1000
        r["lat_p50_ms"] = float(r["lat_p50_us"]) / 1000
    return runs


def group(runs, metric):
    """(shape, key, threads) -> list of metric values over repetitions."""
    g = defaultdict(list)
    for r in runs:
        g[(r["shape"], r["key"], r["threads"])].append(r[metric])
    return g


def label_line_ends(ax, ends):
    """Direct-label each line at its last point; labels are pushed apart so converging lines stay legible."""
    if not ends:
        return
    fig = ax.figure
    fig.canvas.draw()
    to_px = ax.transData.transform
    pts = sorted(((to_px((x, y))[1], x, y, key) for x, y, key in ends))
    gap = 11  # px between label baselines
    placed = []
    for py, x, y, key in pts:
        if placed and py - placed[-1] < gap:
            py = placed[-1] + gap
        placed.append(py)
        dy = py - to_px((x, y))[1]
        ax.annotate(KEYLABEL[key], (x, y), xytext=(6, dy * 72 / fig.dpi), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK)


def by_threads(runs, metric, ylabel, title, fname, logy=False):
    """One panel per table shape: median over reps vs client count, whiskers = min..max."""
    g = group(runs, metric)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True)
    ends_by_ax = []
    for ax, shape in zip(axes, SHAPES):
        ends = []
        ends_by_ax.append((ax, ends))
        for key in KEYS:
            xs, med, lo, hi = [], [], [], []
            for t in THREADS:
                vals = g.get((shape, key, t))
                if vals:
                    xs.append(t)
                    med.append(statistics.median(vals))
                    lo.append(min(vals))
                    hi.append(max(vals))
            if not xs:
                continue
            ax.plot(xs, med, DASH[key], color=COLOR[key], marker="o", markersize=5, label=KEYLABEL[key])
            ax.vlines(xs, lo, hi, color=COLOR[key], linewidth=1, alpha=0.6)
            ends.append((xs[-1], med[-1], key))
        ax.set_xscale("log", base=2)
        ax.set_xticks(THREADS, [str(t) for t in THREADS])
        ax.set_xlim(0.8, 160)
        if logy:
            ax.set_yscale("log")
        ax.set_title(SHAPELABEL[shape], loc="left")
        ax.set_xlabel("concurrent clients (100-row transactions)")
    axes[0].set_ylabel(ylabel)
    if not logy:
        axes[0].set_ylim(bottom=0)
        axes[0].yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))
    axes[1].legend(loc="upper left", bbox_to_anchor=(1.12, 1), frameon=False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    for ax, ends in ends_by_ax:
        label_line_ends(ax, ends)
    fig.savefig(os.path.join(PLOTS, fname), dpi=130)
    plt.close(fig)


def load_ts(config, label):
    path = os.path.join(RESULTS, config, "ts", label + ".ts.csv")
    with open(path) as f:
        ts = list(csv.DictReader(f))
    prev_rows, prev_t, out = 0, 0.0, []
    for p in ts:
        p["_drows"] = int(p["rows"]) - prev_rows
        # The last sample of a run covers only the tail of an interval and is too noisy to plot.
        if float(p["t_s"]) - prev_t >= 0.5:
            out.append(p)
        prev_rows, prev_t = int(p["rows"]), float(p["t_s"])
    return out


def median_run(runs, shape, key, t):
    cands = sorted((r for r in runs if r["shape"] == shape and r["key"] == key and r["threads"] == t),
                   key=lambda r: r["rows_per_s"])
    return cands[len(cands) // 2] if cands else None


def smooth(vals, w=3):
    return [statistics.fmean(vals[max(0, i - w + 1):i + 1]) for i in range(len(vals))]


def timeline(config, runs, per_interval, ylabel, title, fname, threads=(1, 64)):
    """Small multiples (shape x clients): a per-interval quantity against rows inserted so far,
    for the median-throughput repetition of each workload."""
    fig, axes = plt.subplots(len(threads), 2, figsize=(12, 3.6 * len(threads)), sharex=True, squeeze=False)
    ends_by_ax = []
    for i, t in enumerate(threads):
        for j, shape in enumerate(SHAPES):
            ax = axes[i][j]
            ends = []
            ends_by_ax.append((ax, ends))
            for key in KEYS:
                r = median_run(runs, shape, key, t)
                if not r:
                    continue
                ts = load_ts(config, r["label"])
                xs = [int(p["rows"]) / 1e6 for p in ts]
                ys = smooth([per_interval(p) for p in ts])
                ax.plot(xs, ys, DASH[key], color=COLOR[key], label=KEYLABEL[key])
                ends.append((xs[-1], ys[-1], key))
            ax.set_title(f"{SHAPELABEL[shape]}, {t} client{'s' if t > 1 else ''}", loc="left")
            ax.set_ylim(bottom=0)
            ax.set_xlim(0, None)
            if i == len(threads) - 1:
                ax.set_xlabel("rows inserted so far (millions)")
            if j == 0:
                ax.set_ylabel(ylabel)
    axes[0][1].legend(loc="upper left", bbox_to_anchor=(1.12, 1), frameon=False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    for ax, ends in ends_by_ax:
        label_line_ends(ax, ends)
    fig.savefig(os.path.join(PLOTS, fname), dpi=130)
    plt.close(fig)


def per_row(field, scale=1.0):
    def f(p):
        return scale * float(p[field]) / p["_drows"] if p["_drows"] > 0 else 0
    return f


def sizes(runs, title, fname):
    """Bytes per row of each on-disk structure and of log, at 1 client (no concurrency effects)."""
    metrics = [("pk_index_bytes_per_row", "PK index"), ("sec_index_bytes_per_row", "secondary index"),
               ("log_bytes_per_row", "transaction log")]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    for ax, (metric, name) in zip(axes, metrics):
        shape = "pk_sec" if metric == "sec_index_bytes_per_row" else "pk"
        g = group(runs, metric)
        vals = [statistics.median(g[(shape, k, 1)]) if g.get((shape, k, 1)) else 0 for k in KEYS]
        bars = ax.bar(range(len(KEYS)), vals, color=[COLOR[k] for k in KEYS], width=0.7,
                      edgecolor="#fcfcfb", linewidth=2)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:.0f}", (b.get_x() + b.get_width() / 2, v), xytext=(0, 3),
                        textcoords="offset points", ha="center", fontsize=8.5, color=INK)
        ax.set_xticks(range(len(KEYS)), [KEYLABEL[k] for k in KEYS], rotation=20)
        ax.set_title(f"{name} ({SHAPELABEL[shape]})", loc="left")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("bytes per row, 1 client")
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, fname), dpi=130)
    plt.close(fig)


def table(runs, metrics, fmt):
    """Markdown table: one row per (shape, key), one column per (metric, clients)."""
    out = []
    for metric, name in metrics:
        g = group(runs, metric)
        out.append(f"\n**{name}** (median of repetitions)\n")
        out.append("| shape | key | " + " | ".join(f"{t} clients" for t in THREADS) + " |")
        out.append("|---|---|" + "---:|" * len(THREADS))
        for shape in SHAPES:
            for key in KEYS:
                cells = [fmt(statistics.median(g[(shape, key, t)])) if g.get((shape, key, t)) else ""
                         for t in THREADS]
                out.append(f"| {SHAPELABEL[shape]} | {KEYLABEL[key]} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main():
    os.makedirs(PLOTS, exist_ok=True)
    summary = []
    for config, desc in CONFIGS.items():
        runs = load_runs(config)
        if not runs:
            continue
        by_threads(runs, "rows_per_s", "rows inserted per second",
                   f"Insert throughput vs clients — {desc}", f"{config}_throughput.png")
        by_threads(runs, "deadlocks_per_krow", "deadlocks per 1000 rows",
                   f"Deadlocks vs clients — {desc}", f"{config}_deadlocks.png")
        by_threads(runs, "lockwait_us_per_row", "lock-wait time per row (µs)",
                   f"Lock-wait time vs clients — {desc}", f"{config}_lockwait.png")
        by_threads(runs, "lat_p99_ms", "p99 commit latency (ms)",
                   f"p99 latency of a 100-row transaction — {desc}", f"{config}_p99.png")
        timeline(config, runs, lambda p: float(p["rows_per_s"]) / 1000, "thousand rows / s",
                 f"Throughput as the table grows — {desc}", f"{config}_timeline_throughput.png")
        timeline(config, runs, per_row("cache_misses", 1000), "cache misses per 1000 rows",
                 f"Buffer-pool misses as the table grows — {desc}", f"{config}_timeline_cache_misses.png")
        timeline(config, runs, per_row("lockwait_time"), "lock-wait µs per row",
                 f"Lock waits as the table grows — {desc}", f"{config}_timeline_lockwait.png")
        sizes(runs, f"Space per row — {desc}", f"{config}_sizes.png")
        summary.append(f"\n## {config}: {desc}\n")
        summary.append(table(runs, [("rows_per_s", "rows/s")], lambda v: f"{v:,.0f}"))
        summary.append(table(runs, [("lat_p99_ms", "p99 latency, ms")], lambda v: f"{v:.1f}"))
        summary.append(table(runs, [("deadlocks_per_krow", "deadlocks per 1000 rows")], lambda v: f"{v:.2f}"))
        summary.append(table(runs, [("lockwait_us_per_row", "lock-wait µs per row")], lambda v: f"{v:.1f}"))
        summary.append(table(runs, [("cache_misses_per_krow", "cache misses per 1000 rows")], lambda v: f"{v:.1f}"))
        summary.append(table(runs, [("pk_index_bytes_per_row", "PK index bytes per row")], lambda v: f"{v:.1f}"))
        summary.append(table(runs, [("log_bytes_per_row", "log bytes per row")], lambda v: f"{v:.0f}"))
    with open(os.path.join(RESULTS, "summary.md"), "w") as f:
        f.write("# Summary tables (generated by plot.py)\n" + "\n".join(summary) + "\n")


if __name__ == "__main__":
    main()
