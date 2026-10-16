"""Turn results/main/runs.csv and ts/*.csv into PNG charts and summary tables.

usage: python plot.py [results_dir]   (needs matplotlib)
"""
import csv
import glob
import os
import statistics
import sys
from collections import defaultdict

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "results")
RUNDIR = os.path.join(RESULTS, "main")
PLOTS = os.path.join(RESULTS, "plots")
DESC = "single-row inserts, 4M rows, 16MB cache"

# Colour is identity; dashes mark the workloads whose index inserts land at random places.
KEYS = ["v7", "v4", "int_seq", "int_rand", "nextseq", "nextseq_v7", "nextseq_v4"]
COLOR = dict(zip(KEYS, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"]))
RANDOM = {"v4", "int_rand", "nextseq_v4"}
DASH = {k: "--" if k in RANDOM else "-" for k in KEYS}
KEYLABEL = {"v7": "uuid v7 PK", "v4": "uuid v4 PK", "int_seq": "int append PK", "int_rand": "int random PK",
            "nextseq": "nextsequence PK", "nextseq_v7": "nextsequence PK + v7 index",
            "nextseq_v4": "nextsequence PK + v4 index"}
THREADS = [4, 8, 16, 64]

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({
    "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "savefig.facecolor": "#fcfcfb",
    "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 11, "axes.titleweight": "bold", "lines.linewidth": 2,
})
THOUSANDS = matplotlib.ticker.StrMethodFormatter("{x:,.0f}")
PLAIN = matplotlib.ticker.StrMethodFormatter("{x:g}")


def load_runs():
    with open(os.path.join(RUNDIR, "runs.csv")) as f:
        runs = list(csv.DictReader(f))
    for r in runs:
        rows = float(r["rows"])
        r["threads"] = int(r["threads"])
        r["rows_per_s"] = float(r["rows_per_s"])
        r["lat_p50_ms"] = float(r["lat_p50_us"]) / 1000
        r["lat_p99_ms"] = float(r["lat_p99_us"]) / 1000
        r["lat_p999_ms"] = float(r["lat_p999_us"]) / 1000
        r["locks_aborted_per_krow"] = 1000 * float(r["locks_aborted"]) / rows
        r["deadlocks_per_krow"] = 1000 * float(r["deadlocks"]) / rows
        r["lockwait_us_per_row"] = float(r["lockwait_time"]) / rows
        r["cache_misses_per_krow"] = 1000 * float(r["cache_misses"]) / rows
        r["log_bytes_per_row"] = float(r["log_bytes"]) / rows
        r["pk_index_bytes_per_row"] = float(r["pk_index_bytes"]) / rows
        r["second_index_bytes_per_row"] = float(r["second_index_bytes"] or 0) / rows
    return runs


def group(runs, metric):
    """(key, threads) -> list of metric values over repetitions."""
    g = defaultdict(list)
    for r in runs:
        g[(r["key"], r["threads"])].append(r[metric])
    return g


def label_line_ends(ax, ends):
    """Direct-label each line at its last point; labels are pushed apart so converging lines stay legible."""
    if not ends:
        return
    fig = ax.figure
    fig.canvas.draw()
    to_px = ax.transData.transform
    pts = sorted((to_px((x, y))[1], x, y, key) for x, y, key in ends)
    gap = 11  # px between label baselines
    placed = []
    for py, x, y, key in pts:
        if placed and py - placed[-1] < gap:
            py = placed[-1] + gap
        placed.append(py)
        dy = py - to_px((x, y))[1]
        ax.annotate(KEYLABEL[key], (x, y), xytext=(6, dy * 72 / fig.dpi), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK)


def finish(fig, axes_ends, title, fname, legend_ax, legend_x=1.35):
    """legend_x is in legend_ax widths and must clear the direct labels, which take more of a narrow panel."""
    legend_ax.legend(loc="upper left", bbox_to_anchor=(legend_x, 1), frameon=False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    for ax, ends in axes_ends:
        label_line_ends(ax, ends)
    fig.savefig(os.path.join(PLOTS, fname), dpi=130)
    plt.close(fig)


def threads_axis(ax):
    ax.set_xscale("log", base=2)
    ax.set_xticks(THREADS, [str(t) for t in THREADS])
    ax.set_xlim(3.2, 110)
    ax.set_xlabel("concurrent clients (one row per transaction)")


def plot_vs_threads(ax, runs, metric):
    """Median over repetitions vs client count; whiskers = min..max."""
    g = group(runs, metric)
    ends = []
    for key in KEYS:
        pts = [(t, g[(key, t)]) for t in THREADS if g.get((key, t))]
        if not pts:
            continue
        xs = [t for t, _ in pts]
        med = [statistics.median(v) for _, v in pts]
        ax.plot(xs, med, DASH[key], color=COLOR[key], marker="o", markersize=5, label=KEYLABEL[key])
        ax.vlines(xs, [min(v) for _, v in pts], [max(v) for _, v in pts], color=COLOR[key], linewidth=1, alpha=0.6)
        ends.append((xs[-1], med[-1], key))
    threads_axis(ax)
    return ends


def by_threads(runs, metric, ylabel, title, fname):
    fig, ax = plt.subplots(figsize=(10, 5))
    ends = plot_vs_threads(ax, runs, metric)
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(THOUSANDS)
    finish(fig, [(ax, ends)], title, fname, ax)


def latency_vs_threads(runs):
    """p50 and p99 commit latency side by side on a shared log scale."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    axes_ends = []
    for ax, (metric, name) in zip(axes, [("lat_p50_ms", "p50"), ("lat_p99_ms", "p99")]):
        ends = plot_vs_threads(ax, runs, metric)
        # Labels on the left panel would run into the right one; the legend covers it.
        if ax is axes[-1]:
            axes_ends.append((ax, ends))
        ax.set_title(f"{name} commit latency", loc="left")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(PLAIN)
    axes[0].set_ylabel("latency of one insert (ms, log scale)")
    finish(fig, axes_ends, f"Insert latency vs clients — {DESC}", "latency.png", axes[1])


def latency_cdf(runs):
    """Fraction of inserts slower than x, for every insert in all repetitions, one panel per client count.
    A log y axis keeps the tail (p99, p99.9) readable."""
    fig, axes = plt.subplots(1, len(THREADS), figsize=(21, 5.2), sharey=True)
    for ax, t in zip(axes, THREADS):
        for key in KEYS:
            hist = defaultdict(int)
            for r in runs:
                if r["key"] == key and r["threads"] == t:
                    with open(os.path.join(RUNDIR, "ts", r["label"] + ".hist.csv")) as f:
                        for row in csv.DictReader(f):
                            hist[float(row["latency_us"])] += int(row["count"])
            if not hist:
                continue
            xs = sorted(hist)
            total = sum(hist.values())
            above, ys = total, []
            for x in xs:
                above -= hist[x]
                ys.append(max(above, 0.5) / total)
            ax.plot([x / 1000 for x in xs], ys, DASH[key], color=COLOR[key], label=KEYLABEL[key],
                    drawstyle="steps-post")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.xaxis.set_major_formatter(PLAIN)
        ax.set_ylim(1e-5, 1.0)
        for q, name in [(0.5, "p50"), (0.01, "p99"), (0.001, "p99.9")]:
            ax.axhline(q, color=MUTED, linewidth=0.8, linestyle=":")
            ax.annotate(name, (1, q), xycoords=("axes fraction", "data"), xytext=(-4, 2),
                        textcoords="offset points", ha="right", fontsize=8, color=MUTED)
        ax.set_title(f"{t} client{'s' if t > 1 else ''}", loc="left")
        ax.set_xlabel("commit latency (ms, log scale)")
    axes[0].set_ylabel("fraction of inserts slower than x (log scale)")
    axes[-1].legend(loc="upper left", bbox_to_anchor=(1.04, 1), frameon=False)
    fig.suptitle(f"Latency distribution of every insert — {DESC}", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "latency_cdf.png"), dpi=130)
    plt.close(fig)


def load_ts(label):
    with open(os.path.join(RUNDIR, "ts", label + ".ts.csv")) as f:
        ts = list(csv.DictReader(f))
    prev_rows, prev_t, out = 0, 0.0, []
    for p in ts:
        p["_drows"] = int(p["rows"]) - prev_rows
        # The last sample of a run covers only the tail of an interval and is too noisy to plot.
        if float(p["t_s"]) - prev_t >= 0.5:
            out.append(p)
        prev_rows, prev_t = int(p["rows"]), float(p["t_s"])
    return out


def median_run(runs, key, t):
    cands = sorted((r for r in runs if r["key"] == key and r["threads"] == t), key=lambda r: r["rows_per_s"])
    return cands[len(cands) // 2] if cands else None


def smooth(vals, w=3):
    return [statistics.fmean(vals[max(0, i - w + 1):i + 1]) for i in range(len(vals))]


def timeline(runs, per_interval, ylabel, title, fname, logy=False):
    """A per-interval quantity against rows inserted so far, for the median-throughput repetition,
    one panel per client count."""
    fig, axes = plt.subplots(1, len(THREADS), figsize=(21, 5.2), sharex=True)
    axes_ends = []
    for ax, t in zip(axes, THREADS):
        ends = []
        for key in KEYS:
            r = median_run(runs, key, t)
            if not r:
                continue
            ts = load_ts(r["label"])
            xs = [int(p["rows"]) / 1e6 for p in ts]
            ys = smooth([per_interval(p) for p in ts])
            ax.plot(xs, ys, DASH[key], color=COLOR[key], label=KEYLABEL[key])
            ends.append((xs[-1], ys[-1], key))
        ax.set_title(f"{t} client{'s' if t > 1 else ''}", loc="left")
        if logy:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(PLAIN)
        else:
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(THOUSANDS)
        ax.set_xlim(0, None)
        ax.set_xlabel("rows inserted so far (millions)")
        # Labels on the inner panels would run into their neighbours; the legend covers them.
        if ax is axes[-1]:
            axes_ends.append((ax, ends))
    axes[0].set_ylabel(ylabel)
    finish(fig, axes_ends, title, fname, axes[-1], legend_x=1.55)


def per_row(field, scale=1.0):
    def f(p):
        return scale * float(p[field]) / p["_drows"] if p["_drows"] > 0 else 0
    return f


def sizes(runs):
    """Bytes per row of each index and of log, at the lowest client count."""
    t = THREADS[0]
    metrics = [("pk_index_bytes_per_row", "PK index"),
               ("second_index_bytes_per_row", "uuid index (nextsequence tables)"),
               ("log_bytes_per_row", "transaction log")]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    for ax, (metric, name) in zip(axes, metrics):
        g = group(runs, metric)
        keys = [k for k in KEYS
                if g.get((k, t)) and (metric != "second_index_bytes_per_row" or k.startswith("nextseq_"))]
        vals = [statistics.median(g[(k, t)]) for k in keys]
        bars = ax.bar(range(len(keys)), vals, color=[COLOR[k] for k in keys], width=0.7,
                      edgecolor="#fcfcfb", linewidth=2)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:.0f}", (b.get_x() + b.get_width() / 2, v), xytext=(0, 3),
                        textcoords="offset points", ha="center", fontsize=8.5, color=INK)
        ax.set_xticks(range(len(keys)), [KEYLABEL[k] for k in keys], rotation=30, ha="right")
        ax.set_title(name, loc="left")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel(f"bytes per row, {t} clients")
    fig.suptitle(f"Space per row — {DESC}", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "sizes.png"), dpi=130)
    plt.close(fig)


def table(runs, metric, name, fmt):
    g = group(runs, metric)
    out = [f"\n**{name}** (median of repetitions)\n",
           "| workload | " + " | ".join(f"{t} client{'s' if t > 1 else ''}" for t in THREADS) + " |",
           "|---|" + "---:|" * len(THREADS)]
    for key in KEYS:
        cells = [fmt(statistics.median(g[(key, t)])) if g.get((key, t)) else "" for t in THREADS]
        out.append(f"| {KEYLABEL[key]} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def long_timeline():
    """Every repetition of the long v4/v7 run: throughput and cache misses against rows inserted."""
    rundir = os.path.join(RESULTS, "long")
    if not os.path.exists(os.path.join(rundir, "runs.csv")):
        return
    with open(os.path.join(rundir, "runs.csv")) as f:
        runs = list(csv.DictReader(f))
    t = runs[0]["threads"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2), sharex=True)
    for r in runs:
        key = r["key"]
        with open(os.path.join(rundir, "ts", r["label"] + ".ts.csv")) as f:
            ts = list(csv.DictReader(f))
        prev, pts = 0, []
        for p in ts:
            d = int(p["rows"]) - prev
            prev = int(p["rows"])
            if d > 0:
                pts.append((int(p["rows"]) / 1e6, float(p["rows_per_s"]), 1000 * int(p["cache_misses"]) / d))
        pts = pts[:-1]  # the last sample covers only part of an interval
        xs = [x for x, _, _ in pts]
        # One label per key keeps the legend to two entries.
        label = KEYLABEL[key] if r["rep"] == "1" else None
        axes[0].plot(xs, smooth([y for _, y, _ in pts], 60), DASH[key], color=COLOR[key], label=label,
                     linewidth=1.5)
        axes[1].plot(xs, smooth([m for _, _, m in pts], 60), DASH[key], color=COLOR[key], linewidth=1.5)
    axes[0].set_ylabel("rows / s (60 s moving average)")
    axes[0].set_title("throughput", loc="left")
    axes[1].set_ylabel("cache misses per 1000 rows (60 s moving average)")
    axes[1].set_title("buffer-pool misses", loc="left")
    for ax in axes:
        ax.set_xlabel("rows inserted so far (millions)")
        ax.set_xlim(0, None)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(THOUSANDS)
    axes[1].legend(*axes[0].get_legend_handles_labels(), loc="upper left", bbox_to_anchor=(1.04, 1),
                   frameon=False)
    fig.suptitle(f"v4 vs v7 to {int(runs[0]['rows']) // 1_000_000}M rows, {t} clients, 16MB cache, every repetition",
                 x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "long_timeline.png"), dpi=130)
    plt.close(fig)


def main():
    os.makedirs(PLOTS, exist_ok=True)
    for old in glob.glob(os.path.join(PLOTS, "*.png")):
        os.remove(old)
    runs = load_runs()
    by_threads(runs, "rows_per_s", "rows inserted per second", f"Insert throughput vs clients — {DESC}",
               "throughput.png")
    latency_vs_threads(runs)
    latency_cdf(runs)
    by_threads(runs, "locks_aborted_per_krow", "aborted lock requests per 1000 rows",
               f"Aborted lock requests vs clients — {DESC}", "locks_aborted.png")
    by_threads(runs, "lockwait_us_per_row", "lock-wait time per row (µs)",
               f"Lock-wait time vs clients — {DESC}", "lockwait.png")
    timeline(runs, lambda p: float(p["rows_per_s"]), "rows / s",
             f"Throughput as the table grows — {DESC}", "timeline_throughput.png")
    timeline(runs, lambda p: float(p["lat_p99_us"]) / 1000, "p99 latency per second (ms, log scale)",
             f"p99 latency as the table grows — {DESC}", "timeline_p99.png", logy=True)
    timeline(runs, per_row("cache_misses", 1000), "cache misses per 1000 rows",
             f"Buffer-pool misses as the table grows — {DESC}", "timeline_cache_misses.png")
    sizes(runs)
    long_timeline()
    tables = [
        ("rows_per_s", "rows/s", lambda v: f"{v:,.0f}"),
        ("lat_p50_ms", "p50 latency, ms", lambda v: f"{v:.2f}"),
        ("lat_p99_ms", "p99 latency, ms", lambda v: f"{v:.2f}"),
        ("lat_p999_ms", "p99.9 latency, ms", lambda v: f"{v:.1f}"),
        ("locks_aborted_per_krow", "aborted lock requests per 1000 rows", lambda v: f"{v:.1f}"),
        ("deadlocks_per_krow", "deadlocks per 1000 rows", lambda v: f"{v:.2f}"),
        ("lockwait_us_per_row", "lock-wait µs per row", lambda v: f"{v:.1f}"),
        ("cache_misses_per_krow", "cache misses per 1000 rows", lambda v: f"{v:.1f}"),
        ("pk_index_bytes_per_row", "PK index bytes per row", lambda v: f"{v:.1f}"),
        ("second_index_bytes_per_row", "uuid index bytes per row (nextsequence tables)", lambda v: f"{v:.1f}"),
        ("log_bytes_per_row", "log bytes per row", lambda v: f"{v:.0f}"),
    ]
    with open(os.path.join(RESULTS, "summary.md"), "w") as f:
        f.write(f"# Summary tables (generated by plot.py): {DESC}\n")
        for metric, name, fmt in tables:
            f.write(table(runs, metric, name, fmt) + "\n")


if __name__ == "__main__":
    main()
