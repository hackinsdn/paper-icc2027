#!/usr/bin/env python3
"""
Estimate aggregate CPU/Memory capacity needed for N concurrent students,
based on one or more measured Kubernetes resource-usage traces (one trace
per real student who has already completed the lab).

Because different students take different amounts of time to finish (e.g.
50 min vs 100 min), each trace keeps its own duration. Virtual classes of N
students are built by BOOTSTRAP RESAMPLING from the pool of real traces
(sampling with replacement), so both a student's usage profile AND their
pace are preserved. With a single trace this degenerates to the old
"replicate one trace N times" behavior, so single-trace usage still works.

Two scenarios are computed and compared, each as a Monte-Carlo distribution
of the resulting aggregate peak:

  1. SYNCHRONIZED: all N students start at the same time (t=0). Faster
     students drop out of the aggregate once they finish; slower ones keep
     contributing. This is the "everyone starts together" worst case.

  2. STAGGERED (self-paced): each student's start time is additionally
     randomly offset within a configurable window (--stagger-window),
     modeling students who begin the lab at different times.

A deterministic absolute ceiling (N x the single highest peak observed
across all traces, fully aligned) is also reported as a hard reference
bound.

Also reported: the steady-state estimate (N x average usage across the
pool), useful for sizing Kubernetes "requests" (as opposed to burst
capacity / node limits).

Input file format (same as plot_cpu_mem.py), first 4 semicolon-separated
columns used, extra columns ignored:
    timestamp;container_name;cpu_usage;memory_usage

Usage:
    # Multiple individual trace files (one per real student):
    python3 estimate_multi_student.py student1.txt student2.txt student3.txt \
        --students 30 --stagger-window 600 --trials 2000 -o capacity_estimate.png

    # Or point at a directory containing one .txt file per student:
    python3 estimate_multi_student.py traces/ --students 30 -o capacity_estimate.png
"""

import argparse
import glob
import os
import re
import sys
from collections import defaultdict

import numpy as np
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Parsing (same conventions as plot_cpu_mem.py)
# ---------------------------------------------------------------------------

def parse_cpu(value: str) -> float:
    """Convert a Kubernetes CPU quantity string to millicores (float)."""
    value = value.strip()
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000.0


MEM_UNITS = {
    "Ki": 1 / 1024,
    "Mi": 1.0,
    "Gi": 1024.0,
    "Ti": 1024.0 * 1024,
}


def parse_mem(value: str) -> float:
    """Convert a Kubernetes memory quantity string to Mebibytes (Mi, float)."""
    value = value.strip()
    m = re.match(r"^([\d.]+)\s*([A-Za-z]*)$", value)
    if not m:
        raise ValueError(f"cannot parse memory value: {value!r}")
    number, unit = m.groups()
    number = float(number)
    if unit == "":
        return number / (1024 * 1024)
    if unit not in MEM_UNITS:
        raise ValueError(f"unknown memory unit {unit!r} in value {value!r}")
    return number * MEM_UNITS[unit]


def load_single_trace(path: str, round_to: float = 1.0):
    """
    Parse a metrics file for ONE student and aggregate CPU (millicores) and
    Memory (Mi) across all containers belonging to that student, grouped by
    timestamp. Only the first 4 columns of each row are used; extra columns
    are ignored.

    Returns (relative_seconds, cpu_millicores, mem_mi) as numpy arrays, where
    relative_seconds starts at `round_to` (e.g. 1, 2, 3, ...).
    """
    totals_cpu = defaultdict(float)
    totals_mem = defaultdict(float)

    with open(path, "r") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < 4:
                print(f"[warn] {path}:{lineno}: expected at least 4 fields, got {len(parts)} -> skipped", file=sys.stderr)
                continue
            ts_raw, container, cpu_raw, mem_raw = parts[:4]
            try:
                ts = float(ts_raw)
                cpu_m = parse_cpu(cpu_raw)
                mem_mi = parse_mem(mem_raw)
            except ValueError as e:
                print(f"[warn] {path}:{lineno}: {e} -> skipped", file=sys.stderr)
                continue

            key = round(ts / round_to) * round_to
            totals_cpu[key] += cpu_m
            totals_mem[key] += mem_mi

    keys = sorted(totals_cpu.keys())
    if not keys:
        return np.array([]), np.array([]), np.array([])
    t0 = keys[0]
    t = np.array([(k - t0) + round_to for k in keys])
    cpu = np.array([totals_cpu[k] for k in keys])
    mem = np.array([totals_mem[k] for k in keys])
    return t, cpu, mem


def resolve_input_paths(inputs):
    """Expand directories into their *.txt files; keep explicit files as-is."""
    paths = []
    for item in inputs:
        if os.path.isdir(item):
            found = sorted(glob.glob(os.path.join(item, "*.txt")))
            if not found:
                print(f"[warn] no .txt files found in directory {item}", file=sys.stderr)
            paths.extend(found)
        else:
            paths.append(item)
    return paths


# ---------------------------------------------------------------------------
# Resampling onto a uniform grid (zero-order hold, matching how polled
# k8s metrics behave: the last observed value holds until the next sample)
# ---------------------------------------------------------------------------

def resample_hold(t: np.ndarray, values: np.ndarray, dt: float):
    duration = t[-1]
    grid_t = np.arange(0.0, duration + dt, dt)
    idx = np.searchsorted(t, grid_t, side="right") - 1
    idx = np.clip(idx, 0, len(values) - 1)
    grid_values = values[idx]
    return grid_t, grid_values


# ---------------------------------------------------------------------------
# Trace pool
# ---------------------------------------------------------------------------

class TracePool:
    """Holds one resampled (cpu_grid, mem_grid) pair per real student trace."""

    def __init__(self, paths, dt: float, round_to: float):
        self.dt = dt
        self.students = []  # list of dicts: name, cpu_grid, mem_grid, length, duration
        for path in paths:
            t, cpu, mem = load_single_trace(path, round_to=round_to)
            if len(t) == 0:
                print(f"[warn] {path}: no valid data, skipped", file=sys.stderr)
                continue
            grid_t, cpu_grid = resample_hold(t, cpu, dt)
            _, mem_grid = resample_hold(t, mem, dt)
            self.students.append({
                "name": os.path.basename(path),
                "cpu_grid": cpu_grid,
                "mem_grid": mem_grid,
                "length": len(cpu_grid),
                "duration": grid_t[-1] if len(grid_t) else 0.0,
            })
        if not self.students:
            raise ValueError("No valid traces could be loaded from the given input(s).")

    def __len__(self):
        return len(self.students)

    @property
    def max_length(self):
        return max(s["length"] for s in self.students)

    @property
    def max_duration(self):
        return max(s["duration"] for s in self.students)

    def per_trace_stats(self):
        rows = []
        for s in self.students:
            rows.append((s["name"], s["duration"], s["cpu_grid"].max(), s["mem_grid"].max(),
                         s["cpu_grid"].mean(), s["mem_grid"].mean()))
        return rows

    def pooled_avg(self):
        cpu_means = np.array([s["cpu_grid"].mean() for s in self.students])
        mem_means = np.array([s["mem_grid"].mean() for s in self.students])
        return cpu_means.mean(), mem_means.mean()

    def global_peak(self):
        cpu_peak = max(s["cpu_grid"].max() for s in self.students)
        mem_peak = max(s["mem_grid"].max() for s in self.students)
        return cpu_peak, mem_peak


# ---------------------------------------------------------------------------
# Monte Carlo simulation via bootstrap resampling from the trace pool
# ---------------------------------------------------------------------------

def simulate_bootstrap(pool: TracePool, n_students: int, stagger_window: float,
                        trials: int, rng: np.random.Generator, synchronized: bool):
    """
    Build `trials` synthetic classes of `n_students`, each student drawn with
    replacement from the trace pool. If `synchronized` is True all students
    start at t=0 (but still finish at their own pace); otherwise each gets a
    random start offset in [0, stagger_window].

    Returns:
        peaks_cpu, peaks_mem: arrays of length `trials`
        example_t, example_cpu, example_mem: one example simulated aggregate
            (first trial), for plotting
    """
    dt = pool.dt
    max_offset_steps = 0 if synchronized else int(np.ceil(stagger_window / dt))
    total_len = pool.max_length + max_offset_steps
    n_pool = len(pool)

    peaks_cpu = np.empty(trials)
    peaks_mem = np.empty(trials)
    example_cpu = None
    example_mem = None

    for trial in range(trials):
        agg_cpu = np.zeros(total_len)
        agg_mem = np.zeros(total_len)
        idxs = rng.integers(0, n_pool, size=n_students)
        if synchronized:
            offsets_steps = np.zeros(n_students, dtype=int)
        else:
            offsets_steps = (rng.uniform(0, stagger_window, size=n_students) / dt).astype(int)

        for i, off in zip(idxs, offsets_steps):
            s = pool.students[i]
            length = s["length"]
            agg_cpu[off:off + length] += s["cpu_grid"]
            agg_mem[off:off + length] += s["mem_grid"]

        peaks_cpu[trial] = agg_cpu.max()
        peaks_mem[trial] = agg_mem.max()
        if trial == 0:
            example_cpu = agg_cpu.copy()
            example_mem = agg_mem.copy()

    example_t = np.arange(total_len) * dt
    return peaks_cpu, peaks_mem, example_t, example_cpu, example_mem


# ---------------------------------------------------------------------------
# Reporting & plotting
# ---------------------------------------------------------------------------

def fmt_cpu(m):
    return f"{m:,.0f} m ({m/1000:,.2f} cores)"


def fmt_mem(mi):
    return f"{mi:,.0f} Mi ({mi/1024:,.2f} Gi)"


def fmt_min(seconds):
    return f"{seconds/60:,.1f} min"


def print_summary(pool, n_students, cpu_avg, mem_avg, cpu_peak_overall, mem_peak_overall,
                   cpu_peaks_sync, mem_peaks_sync, cpu_peaks_stag, mem_peaks_stag, margin):
    def pct(a, p):
        return np.percentile(a, p)

    print("=" * 78)
    print(f"CAPACITY ESTIMATE FOR {n_students} CONCURRENT STUDENTS "
          f"(bootstrapped from {len(pool)} trace(s) in pool)")
    print("=" * 78)

    durations = [s["duration"] for s in pool.students]
    print(f"\nTrace pool session durations: min = {fmt_min(min(durations))}, "
          f"mean = {fmt_min(np.mean(durations))}, max = {fmt_min(max(durations))}")
    print("\nPer-trace summary:")
    print(f"  {'trace':<28}{'duration':>12}{'peak CPU':>14}{'peak Mem':>14}")
    for name, duration, cpk, mpk, cavg, mavg in pool.per_trace_stats():
        print(f"  {name:<28}{fmt_min(duration):>12}{cpk:>11,.0f} m {mpk:>10,.0f} Mi")

    print(f"\nPooled averages (per student): CPU = {fmt_cpu(cpu_avg)}, Memory = {fmt_mem(mem_avg)}")
    print(f"Highest single-student peak observed in pool: CPU = {fmt_cpu(cpu_peak_overall)}, "
          f"Memory = {fmt_mem(mem_peak_overall)}")

    print("\n--- Steady-state estimate (for k8s 'requests' sizing) ---")
    print(f"  CPU:    {fmt_cpu(cpu_avg * n_students)}")
    print(f"  Memory: {fmt_mem(mem_avg * n_students)}")

    print(f"\n--- Absolute ceiling ({n_students} x highest single-student peak, fully aligned) ---")
    print(f"  CPU:    {fmt_cpu(cpu_peak_overall * n_students)}")
    print(f"  Memory: {fmt_mem(mem_peak_overall * n_students)}")
    print("  (extreme, unrealistic bound: assumes every student peaks like the worst-case trace, at the same instant)")

    print("\n--- Scenario 1: SYNCHRONIZED (all students start together, bootstrap over pool) ---")
    print(f"  CPU  P50: {fmt_cpu(pct(cpu_peaks_sync, 50))}   "
          f"P95: {fmt_cpu(pct(cpu_peaks_sync, 95))}   P99: {fmt_cpu(pct(cpu_peaks_sync, 99))}")
    print(f"  Mem  P50: {fmt_mem(pct(mem_peaks_sync, 50))}   "
          f"P95: {fmt_mem(pct(mem_peaks_sync, 95))}   P99: {fmt_mem(pct(mem_peaks_sync, 99))}")

    print("\n--- Scenario 2: STAGGERED / self-paced (bootstrap + random start offsets) ---")
    print(f"  CPU  P50: {fmt_cpu(pct(cpu_peaks_stag, 50))}   "
          f"P95: {fmt_cpu(pct(cpu_peaks_stag, 95))}   P99: {fmt_cpu(pct(cpu_peaks_stag, 99))}")
    print(f"  Mem  P50: {fmt_mem(pct(mem_peaks_stag, 50))}   "
          f"P95: {fmt_mem(pct(mem_peaks_stag, 95))}   P99: {fmt_mem(pct(mem_peaks_stag, 99))}")

    cpu_recommend = pct(cpu_peaks_stag, 99) * (1 + margin)
    mem_recommend = pct(mem_peaks_stag, 99) * (1 + margin)
    print(f"\n--- Recommended burst capacity target (staggered P99 + {int(margin*100)}% margin) ---")
    print(f"  CPU:    {fmt_cpu(cpu_recommend)}")
    print(f"  Memory: {fmt_mem(mem_recommend)}")
    print("\nNote: if you are not sure students are self-paced, treat SYNCHRONIZED P99 as your")
    print("safety target and the absolute ceiling as a documented worst case; STAGGERED P99 is")
    print("the realistic target if start times are genuinely spread out. Validate with a real")
    print("multi-student load test where possible.")
    if len(pool) < n_students:
        print(f"\n[note] Only {len(pool)} trace(s) were provided for {n_students} students -- "
              "the simulation bootstraps (samples with replacement) from this pool, so add more "
              "traces (ideally real ones) if you can to better capture true student-to-student variability.")
    print("=" * 78)


def make_plots(n_students, example_sync, example_stag,
               cpu_peaks_sync, mem_peaks_sync, cpu_peaks_stag, mem_peaks_stag,
               cpu_ceiling, mem_ceiling, output, title):
    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 10.5,
        "axes.linewidth": 0.8,
        "figure.dpi": 150,
    })
    color_cpu = "#1f77b4"
    color_mem = "#d62728"
    color_sync = "#1f1f1f"
    color_stag = "#ff7f0e"

    sync_t, sync_cpu, sync_mem = example_sync
    stag_t, stag_cpu, stag_mem = example_stag

    fig = plt.figure(figsize=(11, 8))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.1, 1])

    # --- Top: example aggregate timelines, synchronized vs staggered ---
    ax1 = fig.add_subplot(gs[0, :])
    ax1b = ax1.twinx()

    ax1.plot(sync_t, sync_cpu, color=color_cpu, linewidth=1.6,
              label="CPU - synchronized (example)")
    ax1.plot(stag_t, stag_cpu, color=color_cpu, linewidth=1.1, alpha=0.6,
              linestyle=":", label="CPU - staggered (example)")
    ax1b.plot(sync_t, sync_mem, color=color_mem, linewidth=1.6, linestyle="--",
              label="Mem - synchronized (example)")
    ax1b.plot(stag_t, stag_mem, color=color_mem, linewidth=1.1, alpha=0.6,
              linestyle=":", label="Mem - staggered (example)")

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("CPU usage (millicores)", color=color_cpu)
    ax1b.set_ylabel("Memory usage (Mi)", color=color_mem)
    ax1.tick_params(axis="y", labelcolor=color_cpu)
    ax1b.tick_params(axis="y", labelcolor=color_mem)
    ax1.grid(True, linestyle="--", linewidth=0.5, alpha=0.4)
    ax1.set_title(f"{title} - projected for {n_students} students (bootstrapped)")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1b.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8, frameon=False)

    # --- Bottom left: CPU peak distributions (both scenarios overlaid) ---
    ax2 = fig.add_subplot(gs[1, 0])
    ax2.hist(cpu_peaks_sync, bins=40, color=color_sync, alpha=0.55, label="Synchronized")
    ax2.hist(cpu_peaks_stag, bins=40, color=color_stag, alpha=0.55, label="Staggered")
    ax2.axvline(cpu_ceiling, color="red", linestyle="-", linewidth=1.3, label="Absolute ceiling")
    ax2.axvline(np.percentile(cpu_peaks_stag, 99), color=color_stag, linestyle=":", linewidth=1.3, label="Staggered P99")
    ax2.set_xlabel("Aggregate peak CPU (millicores)")
    ax2.set_ylabel("Simulated trials")
    ax2.set_title("Peak CPU distribution")
    ax2.legend(fontsize=7.5, frameon=False)

    # --- Bottom right: Memory peak distributions (both scenarios overlaid) ---
    ax3 = fig.add_subplot(gs[1, 1])
    ax3.hist(mem_peaks_sync, bins=40, color=color_sync, alpha=0.55, label="Synchronized")
    ax3.hist(mem_peaks_stag, bins=40, color=color_stag, alpha=0.55, label="Staggered")
    ax3.axvline(mem_ceiling, color="red", linestyle="-", linewidth=1.3, label="Absolute ceiling")
    ax3.axvline(np.percentile(mem_peaks_stag, 99), color=color_stag, linestyle=":", linewidth=1.3, label="Staggered P99")
    ax3.set_xlabel("Aggregate peak Memory (Mi)")
    ax3.set_ylabel("Simulated trials")
    ax3.set_title("Peak Memory distribution")
    ax3.legend(fontsize=7.5, frameon=False)

    fig.tight_layout()
    fig.savefig(output, dpi=300)
    print(f"\nSaved figure to {output}")
    if output.lower().endswith(".png"):
        vector_path = output[:-4] + ".pdf"
        fig.savefig(vector_path)
        print(f"Saved vector version to {vector_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Estimate multi-student CPU/Memory capacity by bootstrapping from one or more real student traces.")
    parser.add_argument("inputs", nargs="+",
                         help="One or more trace .txt files (one per real student), and/or a directory containing them.")
    parser.add_argument("--students", type=int, default=30, help="Number of concurrent students to project (default: 30)")
    parser.add_argument("--stagger-window", type=float, default=None,
                         help="Window (seconds) over which student start times are randomly spread in the "
                              "staggered scenario. Default: the longest observed session duration in the pool.")
    parser.add_argument("--trials", type=int, default=2000, help="Number of Monte Carlo trials per scenario (default: 2000)")
    parser.add_argument("--dt", type=float, default=1.0, help="Grid resolution in seconds for resampling/simulation (default: 1)")
    parser.add_argument("--round-to", type=float, default=1.0, help="Seconds to round timestamps to when aggregating containers within a trace (default: 1)")
    parser.add_argument("--margin", type=float, default=0.25, help="Safety margin fraction added to the recommended target (default: 0.25 = 25%%)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("-o", "--output", default="capacity_estimate.png", help="Output figure path (PNG); a .pdf twin is also saved")
    parser.add_argument("--title", default="CPU and Memory usage", help="Base title for the figure")
    args = parser.parse_args()

    paths = resolve_input_paths(args.inputs)
    if not paths:
        print("No input trace files found.", file=sys.stderr)
        sys.exit(1)

    pool = TracePool(paths, dt=args.dt, round_to=args.round_to)

    n = args.students
    stagger_window = args.stagger_window if args.stagger_window is not None else pool.max_duration

    cpu_avg, mem_avg = pool.pooled_avg()
    cpu_peak_overall, mem_peak_overall = pool.global_peak()

    rng_sync = np.random.default_rng(args.seed)
    cpu_peaks_sync, mem_peaks_sync, sync_t, sync_cpu_ex, sync_mem_ex = simulate_bootstrap(
        pool, n, stagger_window, args.trials, rng_sync, synchronized=True)

    rng_stag = np.random.default_rng(args.seed + 1)
    cpu_peaks_stag, mem_peaks_stag, stag_t, stag_cpu_ex, stag_mem_ex = simulate_bootstrap(
        pool, n, stagger_window, args.trials, rng_stag, synchronized=False)

    print_summary(pool, n, cpu_avg, mem_avg, cpu_peak_overall, mem_peak_overall,
                  cpu_peaks_sync, mem_peaks_sync, cpu_peaks_stag, mem_peaks_stag, args.margin)

    make_plots(n, (sync_t, sync_cpu_ex, sync_mem_ex), (stag_t, stag_cpu_ex, stag_mem_ex),
               cpu_peaks_sync, mem_peaks_sync, cpu_peaks_stag, mem_peaks_stag,
               cpu_peak_overall * n, mem_peak_overall * n,
               args.output, args.title)


if __name__ == "__main__":
    main()
