#!/usr/bin/env python3
"""
plot_concurrent_instances.py

Computes the number of simultaneous ("concurrent") records in a SQLite
table over time -- based on a created_at / updated_at interval per row --
and renders a publication-quality PDF chart showing, per day:

    - peak concurrency reached that day
    - time-weighted average concurrency for that day

Unlike a naive "average the concurrency value at each event timestamp"
approach, the average here is computed as a true time integral of the
step function (concurrency level x duration held), divided by the
elapsed time covered that day. This gives an honest daily average that
isn't skewed by uneven event spacing.

Usage
-----
    python plot_concurrent_instances.py path/to/database.db \
        --table lab_instances \
        --output concurrency_report.pdf

Optional flags
--------------
    --created-col   Name of the "start" timestamp column (default: created_at)
    --updated-col   Name of the "end" timestamp column   (default: updated_at)
    --as-of         ISO timestamp to use as "now" for rows where updated_at
                     IS NULL (still running). Defaults to the current time.
    --title         Custom chart title
    --dpi           Output resolution for any rasterized elements (default 300)

Requirements
------------
    pip install matplotlib --break-system-packages
    (sqlite3 is part of the Python standard library)
"""

import json
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def parse_ts(value):
    """Parse a SQLite DATETIME string into a Python datetime object."""
    if value is None:
        return None
    # Try a handful of common SQLite datetime formats.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse timestamp: {value!r}")


def load_intervals(db_path, table, created_col, updated_col):
    """Return a list of (created_at, updated_at_or_None) datetime tuples."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {created_col}, {updated_col} FROM {table}")
        rows = cur.fetchall()
    finally:
        conn.close()

    intervals = []
    for created_raw, updated_raw in rows:
        created = parse_ts(created_raw)
        updated = parse_ts(updated_raw)
        if created is None:
            continue  # skip rows with no start time -- nothing to plot
        intervals.append((created, updated))
    return intervals


# --------------------------------------------------------------------------
# Concurrency computation
# --------------------------------------------------------------------------

def build_step_function(intervals, as_of):
    """
    Turn (start, end) intervals into a sorted list of (timestamp, level)
    describing the concurrency step function. `end=None` rows are treated
    as still open, capped at `as_of`.

    Tie-breaking: a "+1" event is applied before a "-1" event at the exact
    same timestamp, so a record ending the instant another begins counts
    as briefly overlapping. Flip the sort key below if you want the
    opposite convention.
    """
    events = []
    for start, end in intervals:
        end = end if end is not None else as_of
        if end < start:
            # Defensive: skip malformed rows where end precedes start.
            continue
        events.append((start, 1))
        events.append((end, -1))

    # +1 before -1 at equal timestamps -> sort by (time, -delta)
    events.sort(key=lambda e: (e[0], -e[1]))

    step = []
    level = 0
    for ts, delta in events:
        level += delta
        step.append((ts, level))
    return step


def daily_stats(step):
    """
    Walk the (timestamp, level) step function and compute, per calendar
    day:
        - peak concurrency observed
        - time-weighted average concurrency (integral / elapsed seconds)

    Returns a dict: {date: {"peak": int, "avg": float}}
    """
    if not step:
        return {}

    peak = defaultdict(int)
    weighted_seconds = defaultdict(float)
    covered_seconds = defaultdict(float)

    # level_before is the concurrency level holding *before* each event;
    # we integrate the level over [prev_ts, ts) intervals.
    prev_ts = step[0][0]
    prev_level = 0

    for ts, level in step:
        if ts > prev_ts:
            _accumulate_interval(prev_ts, ts, prev_level,
                                  peak, weighted_seconds, covered_seconds)
        prev_ts = ts
        prev_level = level

    # Register the peak at the very last instant too (in case the last
    # event bumps concurrency down but we still want that instant counted).
    day = prev_ts.date()
    peak[day] = max(peak[day], prev_level)

    result = {}
    for day, secs in covered_seconds.items():
        avg = weighted_seconds[day] / secs if secs > 0 else 0.0
        result[day] = {"peak": peak[day], "avg": avg}
    return result


def _accumulate_interval(t0, t1, level, peak, weighted_seconds, covered_seconds):
    """Split [t0, t1) at midnight boundaries and accumulate per-day stats."""
    cur = t0
    while cur < t1:
        day_end = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
        segment_end = min(t1, day_end)
        duration = (segment_end - cur).total_seconds()

        day = cur.date()
        weighted_seconds[day] += level * duration
        covered_seconds[day] += duration
        peak[day] = max(peak[day], level)

        cur = segment_end


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------

# Named color palettes: (peak_color, average_color)
PALETTES = {
    "mono":  ("black",   "black"),
    "blue":  ("#1f3864", "#4f9ecc"),   # deep navy + lighter sky blue
    "green": ("#1e5631", "#7fb069"),   # deep forest green + lighter sage
    "mixed": ("#1f3864", "#7fb069"),
}


def plot_daily_stats(stats, output_path, title=None, dpi=300, palette="mono"):
    if not stats:
        print("No data to plot.", file=sys.stderr)
        sys.exit(1)

    days = sorted(stats.keys())
    peaks = [stats[d]["peak"] for d in days]
    avgs = [stats[d]["avg"] for d in days]
    dates = [datetime(d.year, d.month, d.day) for d in days]

    # Publication-style settings: serif font, modest figure size, vector output.
    plt.rcParams.update({
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "font.serif": ['Times New Roman', 'Times', 'Nimbus Roman'],
        "font.family": "serif",
        "font.size": 14,
        "axes.labelsize": 14,
        "axes.titlesize": 14,
        "legend.fontsize": 14,
        "xtick.labelsize": 14,
        "ytick.labelsize": 14,
        "axes.linewidth": 0.8,
        "figure.dpi": dpi,
    })

    peak_color, avg_color = PALETTES.get(palette, PALETTES["mono"])

    fig, ax = plt.subplots(figsize=(8, 3.0))

    ax.step(dates, peaks, where="post", label="Peak concurrent records",
             color=peak_color, linewidth=1.6)
    ax.plot(dates, avgs, label="Time-weighted average",
            color=avg_color, linewidth=1.4, linestyle="--", marker="o",
            markersize=3.5)

    #ax.set_xlabel("Date")
    ax.set_ylabel("Concurrent Lab Instances")
    #ax.set_title(title or "Concurrent Records Over Time")
    ax.grid(True, which="major", axis="both", linestyle=":", linewidth=0.6,
            color="gray", alpha=0.6)
    ax.legend(frameon=False, loc="upper left")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate(rotation=25)

    # Integer y-axis ticks since concurrency counts are whole numbers.
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    fig.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", help="Path to the SQLite .db file")
    parser.add_argument("--table", default="lab_instances",
                         help="Table name (default: lab_instances)")
    parser.add_argument("--created-col", default="created_at",
                         help="Start timestamp column (default: created_at)")
    parser.add_argument("--updated-col", default="updated_at",
                         help="End timestamp column (default: updated_at)")
    parser.add_argument("--as-of", default=None,
                         help="ISO timestamp treated as 'now' for still-open "
                              "rows (default: current time)")
    parser.add_argument("--output", default="concurrency_report.pdf",
                         help="Output PDF path (default: concurrency_report.pdf)")
    parser.add_argument("--title", default=None, help="Custom chart title")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--palette", default="mono", choices=list(PALETTES.keys()),
                         help="Color palette: mono (black/grayscale, default), "
                              "blue, or green")
    args = parser.parse_args()

    as_of = parse_ts(args.as_of) if args.as_of else datetime.now()

    if args.db_path.endswith("json"):
        intervals = json.load(open(args.db_path))
        intervals = [
            (datetime.fromisoformat(a), datetime.fromisoformat(b))
            for a, b in intervals
        ]
    else:
        intervals = load_intervals(args.db_path, args.table,
                                args.created_col, args.updated_col)
        with open(f"{args.db_path}.json", "w") as f:
            f.write(json.dumps(intervals, default=str))
    if not intervals:
        print("No rows found -- nothing to plot.", file=sys.stderr)
        sys.exit(1)

    step = build_step_function(intervals, as_of)
    stats = daily_stats(step)
    plot_daily_stats(stats, args.output, title=args.title, dpi=args.dpi,
                      palette=args.palette)


if __name__ == "__main__":
    main()
