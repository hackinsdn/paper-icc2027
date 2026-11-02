#!/usr/bin/env python3
"""Three-panel tunnel comparison; --simulate fills missing scenarios explicitly.

Example preview:
  python3 plot-tunnel-scenarios.py --same-pod /path/ids-es --simulate
Final measured figure:
  python3 plot-tunnel-scenarios.py --same-pod /path/ids-es \
    --same-node /path/pod-results --different-nodes /path/cross-node-results
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
from matplotlib.patches import Patch
import numpy as np
from scipy.stats import t

TECHS = ("baseline", "vxlan", "l2tpv3")
LABELS = ("Baseline", "VXLAN", "L2TPv3")
COLORS = ("#56B4E9", "#E69F00", "#009E73")
HATCHES = ("///", "\\\\", "xx")


def load(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path /= "summary.csv"
    values = {tech: [] for tech in TECHS}
    seen, failures = set(), []
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"round", "technology", "status", "receiver_bps"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: missing benchmark columns")
        for row in reader:
            tech = {"veth": "baseline", "pod-cni": "baseline"}.get(row["technology"], row["technology"])
            if tech not in values:
                raise ValueError(f"Unknown technology: {row['technology']}")
            key = (row["round"], tech)
            if key in seen:
                raise ValueError(f"Duplicate round/technology: {key}")
            seen.add(key)
            if row["status"] != "ok":
                failures.append(dict(row))
                continue
            value = float(row["receiver_bps"])/1e9
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"Invalid receiver throughput: {key}")
            values[tech].append(value)
    return values, dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), failed_runs=failures)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--same-pod", required=True, help="Measured ids-es CSV or folder")
    p.add_argument("--same-node", help="Measured pod-to-pod, same-node CSV or folder")
    p.add_argument("--different-nodes", help="Measured cross-node CSV or folder")
    p.add_argument("--simulate", action="store_true", help="Fill missing scenarios with explicitly labeled synthetic data")
    p.add_argument("--compact", action="store_true", help="Paper layout without overall title, sample counts, or explanatory footer")
    p.add_argument("--seed", type=int, default=20261030)
    p.add_argument("--output", default="figures/tunnel-scenarios-preview")
    args = p.parse_args()
    rng = np.random.default_rng(args.seed)
    specs = [("Namespaces in one pod", args.same_pod, None),
             ("Pods on the same node", args.same_node, (18.0, 13.0, 2.1)),
             ("Pods on different nodes", args.different_nodes, (0.94, 0.89, 0.80))]
    datasets, records, provenance = [], [], []
    try:
        for scenario, source, mock_means in specs:
            simulated = source is None
            if simulated:
                if not args.simulate:
                    raise ValueError(f"Missing measured input for '{scenario}'; use --simulate for a preview")
                # Arbitrary illustrative values, NOT estimates of future results.
                values = {tech: rng.normal(mean, mean*0.025, 10).tolist()
                          for tech, mean in zip(TECHS, mock_means)}
                info = dict(seed=args.seed, illustrative_only=True)
            else:
                values, info = load(source)
            rows = []
            for tech in TECHS:
                samples = np.asarray(values[tech], dtype=float)
                n = len(samples)
                if n < 2:
                    raise ValueError(f"{scenario}/{tech}: need at least two successful runs")
                mean, sd = float(samples.mean()), float(samples.std(ddof=1))
                half = float(t.ppf(0.975, n-1)*sd/np.sqrt(n))
                row = dict(scenario=scenario, provenance="SIMULATED" if simulated else "MEASURED",
                           technology=tech, n=n, mean_gbps=mean, sd_gbps=sd,
                           ci95_low_gbps=mean-half, ci95_high_gbps=mean+half,
                           ci95_half_width_gbps=half)
                rows.append(row)
                records.append(row)
            datasets.append((scenario, simulated, rows))
            provenance.append(dict(scenario=scenario, simulated=simulated, samples_gbps=values, **info))
            if info.get("failed_runs"):
                print(f"WARNING: failed runs excluded in {scenario}; see metadata", file=sys.stderr)
    except (ValueError, OSError, KeyError) as exc:
        p.error(str(exc))

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "pdf.fonttype": 42, "svg.fonttype": "none", "hatch.linewidth": 0.65,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 3, figsize=(11.0, 2.8 if args.compact else 4.5))
    fig.subplots_adjust(left=0.07, right=0.985, bottom=0.16 if args.compact else 0.25,
                        top=0.78 if args.compact else 0.75, wspace=0.4)
    for index, (ax, (scenario, simulated, rows)) in enumerate(zip(axes, datasets)):
        for j, row in enumerate(rows):
            mean, half = row["mean_gbps"], row["ci95_half_width_gbps"]
            ax.bar(j, mean, width=0.64, yerr=half, color=COLORS[j], hatch=HATCHES[j],
                   edgecolor="#333333", linewidth=0.8, capsize=4,
                   error_kw={"elinewidth": 1.1, "capthick": 1.1}, zorder=3)
            ax.annotate(f"{mean:.2f}", (j, mean+half), xytext=(0, 6),
                        textcoords="offset points", ha="center", va="bottom", fontsize=9)
        high = max(r["ci95_high_gbps"] for r in rows)
        low = min(0, min(r["ci95_low_gbps"] for r in rows))
        ax.set_ylim(low*1.1, high*1.22)
        ax.set_xticks(range(3), LABELS, rotation=0)
        ax.set_ylabel("Receiver throughput (Gbit/s)")
        ax.grid(axis="y", alpha=0.2, zorder=0)
        status = "SIMULATED — illustrative" if simulated else "MEASURED"
        title = f"({chr(97+index)}) {scenario}"
        if args.compact and simulated:
            title += " [SIMULATED]"
        ax.set_title(title, fontsize=10, pad=8 if args.compact else 31)
        if not args.compact:
            ax.text(0.5, 1.035, status, transform=ax.transAxes, ha="center", fontsize=8,
                    color="#9B3C00" if simulated else "#333333", fontweight="bold")
            counts = sorted(set(r["n"] for r in rows))
            ax.text(0.5, -0.20, "n="+"/".join(map(str, counts))+" runs per technology",
                    transform=ax.transAxes, ha="center", fontsize=8, color="#444444")
    has_simulated = any(sim for _, sim, _ in datasets)
    if not args.compact:
        fig.suptitle("Tunnel throughput comparison" + (" — layout preview" if has_simulated else ""),
                     y=0.985, fontsize=13)
    fig.legend(handles=[Patch(facecolor=c, edgecolor="#333333", hatch=h, label=l)
                        for c, h, l in zip(COLORS, HATCHES, ("Baseline/no-tunnel", "VXLAN", "L2TP"))],
               loc="upper center", bbox_to_anchor=(0.5, 1.02 if args.compact else 0.925),
               ncol=3, frameon=False, fontsize=9)
    footer = "Bars: means; error bars: Student-t 95% CIs. Independent linear y-axis scales.\n"
    footer += ("Panels (b) and/or (c) labeled SIMULATED use synthetic data and CIs, not measurements."
               if has_simulated else "Baseline: direct veth within one pod; existing CNI path between pods.")
    if not args.compact:
        fig.text(0.5, 0.035, footer, ha="center", fontsize=8, linespacing=1.6)
    prefix = Path(args.output).expanduser()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(str(prefix)+"."+ext, dpi=300)
    plt.close(fig)
    with Path(str(prefix)+"-statistics.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    metadata = dict(method="mean +/- t(0.975,n-1) * sample_SD(ddof=1) / sqrt(n)",
                    interpretation="Repeated-run CI assumes independent observations; synthetic panels have no empirical interpretation.",
                    baseline="Direct veth in one pod; CNI path between pods. Never pool these scenarios.",
                    scenarios=provenance)
    Path(str(prefix)+"-metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    print(f"Saved {prefix}.png / .pdf / .svg, statistics CSV, and provenance JSON")


if __name__ == "__main__":
    main()
