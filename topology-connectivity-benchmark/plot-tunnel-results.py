#!/usr/bin/env python3
"""Plot benchmark summary.csv files: mean throughput and Student-t 95% CIs.

Example:
  python3 plot-tunnel-results.py --input ids-es=/path/es/summary.csv \
    --input ids-mg=/path/mg/summary.csv --output figures/tunnel-throughput
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy
from scipy.stats import t

TECHNOLOGIES = ("veth", "vxlan", "l2tpv3")
LABELS = ("Direct veth", "VXLAN", "L2TPv3/UDP")


def read_results(spec):
    label, separator, filename = spec.partition("=")
    if not separator or not label or not filename:
        raise ValueError("Each --input must be NODE=/path/to/summary.csv")
    path = Path(filename).expanduser().resolve()
    if path.is_dir():
        path /= "summary.csv"
    groups = {name: [] for name in TECHNOLOGIES}
    rounds = set()
    failed = []
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"round", "technology", "status", "receiver_bps"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: missing columns {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            tech = row["technology"]
            if tech not in groups:
                raise ValueError(f"{path}: unexpected technology {tech!r}; expected {TECHNOLOGIES}")
            key = (row["round"], tech)
            if key in rounds:
                raise ValueError(f"{path}: duplicate round/technology {key}")
            rounds.add(key)
            if row["status"] != "ok":
                failed.append({"round": row["round"], "technology": tech, "status": row["status"]})
                continue
            value = float(row["receiver_bps"]) / 1e9
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{path}: invalid throughput for {key}")
            groups[tech].append(value)
    stats = []
    for tech, values in groups.items():
        if len(values) < 2:
            raise ValueError(f"{label}/{tech}: at least two successful runs are required for a CI")
        samples = np.asarray(values)
        mean = float(samples.mean())
        sd = float(samples.std(ddof=1))
        half = float(t.ppf(0.975, len(samples) - 1) * sd / np.sqrt(len(samples)))
        stats.append(dict(node=label, technology=tech, n=len(samples),
                          mean_gbps=mean, sd_gbps=sd,
                          ci95_low_gbps=mean-half, ci95_high_gbps=mean+half,
                          ci95_half_width_gbps=half))
    return label, path, stats, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", action="append", required=True, metavar="NODE=CSV")
    parser.add_argument("--output", default="figures/tunnel-throughput", help="Output filename prefix")
    parser.add_argument("--title", default="", help="Optional figure title; omitted by default for papers")
    parser.add_argument("--show-points", action="store_true", help="Overlay individual run measurements")
    args = parser.parse_args()
    try:
        datasets = [read_results(spec) for spec in args.input]
        labels = [item[0] for item in datasets]
        if len(labels) != len(set(labels)):
            raise ValueError("Node labels must be unique")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    records = [record for _, _, stats, _ in datasets for record in stats]
    with Path(str(output) + "-statistics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "pdf.fonttype": 42, "ps.fonttype": 42,
                         "svg.fonttype": "none", "axes.spines.top": False,
                         "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(7.2, 4.5), layout="constrained")
    x = np.arange(len(TECHNOLOGIES))
    width = 0.76 / len(datasets)
    colors = ("#0072B2", "#E69F00", "#009E73", "#CC79A7")
    for index, (label, path, stats, failed) in enumerate(datasets):
        centers = x + (index - (len(datasets)-1)/2) * width
        means = np.array([r["mean_gbps"] for r in stats])
        errors = np.array([r["ci95_half_width_gbps"] for r in stats])
        ax.bar(centers, means, width=width*0.92, yerr=errors,
               label=label, color=colors[index % len(colors)], edgecolor="#333333",
               linewidth=0.5, capsize=4, error_kw={"elinewidth": 1.1})
        for center, mean, error, record in zip(centers, means, errors, stats):
            ax.annotate(f"{mean:.2f}\n(n={record['n']})", (center, mean+error),
                        xytext=(0, 5), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8)
        if args.show_points:
            # Read back measurements to overlay points with deterministic spacing.
            with path.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            for center, tech in zip(centers, TECHNOLOGIES):
                values = [float(r["receiver_bps"])/1e9 for r in rows
                          if r["technology"] == tech and r["status"] == "ok"]
                offsets = np.linspace(-width*0.22, width*0.22, len(values))
                ax.scatter(center+offsets, values, s=10, color="#222222", alpha=0.5, zorder=4)
        if failed:
            print(f"WARNING: {label}: {len(failed)} failed runs excluded from mean/CI; see metadata.", file=sys.stderr)
    ax.set_xticks(x, LABELS)
    ax.set_ylabel("Receiver TCP throughput (Gbit/s)")
    ax.set_axisbelow(True)
    ax.grid(axis="y", alpha=0.22)
    low = min(0, min(r["ci95_low_gbps"] for r in records))
    high = max(r["ci95_high_gbps"] for r in records)
    ax.set_ylim(low*1.1, max(high*1.23, 1))
    ax.legend(title="Node", frameon=False, loc="upper right")
    if args.title:
        ax.set_title(args.title, pad=12)
    fig.supxlabel("Bars: run means; error bars: two-sided Student-t 95% CIs", fontsize=8)
    for extension in ("png", "pdf", "svg"):
        fig.savefig(str(output) + "." + extension, dpi=300)
    plt.close(fig)
    metadata = dict(
        metric="Per-run receiver throughput, receiver_bps / 1e9",
        method="mean +/- t.ppf(0.975, n-1) * sample_SD(ddof=1) / sqrt(n)",
        assumptions="Independent run means with approximately normal sampling distribution; systematic errors and host/time effects are not covered.",
        filtering="Only status=ok included; no outlier removal; failed runs listed below.",
        versions=dict(python=sys.version, numpy=np.__version__, scipy=scipy.__version__, matplotlib=matplotlib.__version__),
        inputs=[dict(node=label, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     failed_runs=failed) for label, path, _, failed in datasets])
    Path(str(output)+"-metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    for record in records:
        print(f"{record['node']:12s} {record['technology']:8s} n={record['n']:2d} "
              f"{record['mean_gbps']:.3f} Gbit/s "
              f"[95% CI {record['ci95_low_gbps']:.3f}, {record['ci95_high_gbps']:.3f}]")
    print(f"Saved: {output}.png / .pdf / .svg, statistics CSV, and metadata JSON")


if __name__ == "__main__":
    main()
