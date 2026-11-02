#!/usr/bin/env python3
"""Combine saved throughput and RTT plot metadata into a compact 2x3 figure.
Uses the exact per-run samples/provenance from the two previous figures.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
from scipy.stats import t

TECHS = ("baseline", "vxlan", "l2tpv3")
COLORS = ("#56B4E9", "#E69F00", "#009E73")
HATCHES = ("///", "\\\\", "xx")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--throughput", default="figures/tunnel-scenarios-final-metadata.json")
    p.add_argument("--latency-pps", default="figures/latency-pps-scenarios-preview-metadata.json")
    p.add_argument("--output", default="figures/all-metrics-scenarios-preview")
    args = p.parse_args()
    throughput_path = Path(args.throughput).expanduser().resolve()
    latency_path = Path(args.latency_pps).expanduser().resolve()
    throughput = json.loads(throughput_path.read_text())
    latency = json.loads(latency_path.read_text())
    scenarios = [s["scenario"] for s in throughput["scenarios"]]
    latency_by_name = {s["scenario"]: s for s in latency["scenarios"]}
    if len(scenarios) != 3 or set(scenarios) != set(latency_by_name):
        p.error("The two metadata files must describe the same three scenarios")
    metrics = (("throughput", "Tput (Gbps)"),
               ("ping", "RTT (ms)"))
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 16,
                         "pdf.fonttype": 42, "svg.fonttype": "none", "hatch.linewidth": .65,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(len(metrics), 3, figsize=(7.2, 4.0))
    fig.subplots_adjust(left=.13, right=.985, bottom=.08, top=.80, wspace=.55, hspace=.30)
    records = []
    for col, name in enumerate(scenarios):
        tp = throughput["scenarios"][col]
        lp = latency_by_name[name]
        for row, (metric, ylabel) in enumerate(metrics):
            ax = axes[row, col]
            samples_by_tech = tp["samples_gbps"] if row == 0 else lp["samples"][metric]
            simulated = tp["simulated"] if row == 0 else lp["simulated"]
            high, low = 0., 0.
            for index, tech in enumerate(TECHS):
                samples = np.asarray(samples_by_tech[tech], dtype=float)
                if samples.ndim != 1 or len(samples) == 0 or not np.all(np.isfinite(samples)) or np.any(samples < 0):
                    p.error(f"Invalid samples: {name}/{metric}/{tech}")
                n, mean = len(samples), float(samples.mean())
                sd = float(samples.std(ddof=1)) if n > 1 else None
                half = float(t.ppf(.975, n-1)*sd/np.sqrt(n)) if n > 1 else None
                ax.bar(index, mean, width=.64, yerr=half, capsize=3,
                       color=COLORS[index], hatch=HATCHES[index], edgecolor="#333333",
                       linewidth=.7, error_kw={"elinewidth": 1}, zorder=3)
                value = f"{mean:.3f}" if metric == "ping" and mean < 1 else f"{mean:.2f}"
                ax.annotate(value, (index, mean+(half or 0)), xytext=(0, 4),
                            textcoords="offset points", ha="center", fontsize=12)
                high, low = max(high, mean+(half or 0)), min(low, mean-(half or 0))
                records.append(dict(scenario=name, metric=metric, unit=ylabel,
                                    provenance="SIMULATED" if simulated else "MEASURED", technology=tech,
                                    n=n, mean=mean, sd=sd, ci95_low=mean-half if half is not None else None,
                                    ci95_high=mean+half if half is not None else None))
            ax.set_ylim(low*1.1, max(high*1.32, .001))
            ax.set_xticks([])  # Technologies are identified by the shared legend.
            if col == 0:
                ax.set_ylabel(ylabel, fontsize=14)
            ax.tick_params(axis="y", labelsize=14)
            ax.locator_params(axis="y", nbins=3)
            ax.grid(axis="y", alpha=.2, zorder=0)
            if row == 0:
                title = ("Namespaces", "Same node", "Diff nodes")[col]
                ax.set_title(f"({chr(97+col)}) {title}", fontsize=14, pad=10)
            if simulated:
                ax.text(.98, .98, "SIMULATED", transform=ax.transAxes,
                        ha="right", va="top", fontsize=7, fontweight="bold", color="#9B3C00")
    fig.legend(handles=[Patch(facecolor=c, edgecolor="#333333", hatch=h, label=label)
                        for c, h, label in zip(COLORS, HATCHES, ("Baseline/no-tunnel", "VXLAN", "L2TP"))],
               loc="upper center", bbox_to_anchor=(.5, 1.0), ncol=3, frameon=False, fontsize=14,
               handlelength=1.5, columnspacing=1.0, handletextpad=.4)
    prefix = Path(args.output).expanduser()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "pdf", "svg"):
        fig.savefig(str(prefix)+"."+extension, dpi=300)
    plt.close(fig)
    with Path(str(prefix)+"-statistics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    metadata = dict(inputs=[dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                            for path in (throughput_path, latency_path)],
                    throughput_metadata=throughput, latency_pps_metadata=latency,
                    method="Run means and Student-t 95% CIs with n>=2; otherwise no CI. Synthetic cells are explicitly labeled.",
                    axes="Independent linear y-axis scales per subplot; units indicated on every panel.",
                    interpretation="Throughput and idle RTT were measured in separate experiments.",
                    statistics=records)
    Path(str(prefix)+"-metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    print(f"Saved {prefix}.png / .pdf / .svg and statistics / provenance")


if __name__ == "__main__":
    main()
