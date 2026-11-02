#!/usr/bin/env python3
"""RTT and approximate receiver PPS across three scenarios.
Use --simulate to fill missing scenario folders with labeled synthetic data.
"""
import argparse
import csv
import hashlib
import io
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
COLORS = ("#56B4E9", "#E69F00", "#009E73")
HATCHES = ("///", "\\\\", "xx")
METRICS = (("ping", "mean_ms", 1, "Mean RTT (ms)"),
           ("pps", "receiver_interface_pps_approx", 1000, "Receiver rate (kpacket/s)"))


def measured(folder):
    folder = Path(folder).expanduser().resolve()
    values, provenance, snapshots = {}, [], {}
    for kind, field, divisor, _ in METRICS:
        path = folder / f"{kind}-summary.csv"
        raw = path.read_bytes()
        snapshots[kind] = raw
        rows = list(csv.DictReader(io.StringIO(raw.decode())))
        valid = {"ok", "partial"} if kind == "ping" else {"interface_activity_only"}
        groups = {tech: [] for tech in TECHS}
        seen, excluded = set(), []
        for row in rows:
            tech = {"veth": "baseline", "pod-cni": "baseline"}.get(row["technology"], row["technology"])
            key = (row["round"], tech)
            if key in seen or tech not in groups:
                raise ValueError(f"{path}: duplicate or unknown technology: {key}")
            seen.add(key)
            if row["status"] not in valid:
                excluded.append(row)
                continue
            value = float(row[field])/divisor
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{path}: invalid numeric value for {key}")
            groups[tech].append(value)
        values[kind] = groups
        modes = sorted({row.get("source_mode", "legacy-spoof") for row in rows}) if kind == "pps" else []
        if len(modes) > 1:
            raise ValueError(f"{path}: mixed source modes")
        provenance.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                               source_modes=modes, excluded_rows=excluded))
    return values, provenance, snapshots


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--same-pod")
    p.add_argument("--same-node")
    p.add_argument("--different-nodes", required=True)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--seed", type=int, default=20261030)
    p.add_argument("--output", default="figures/latency-pps-scenarios-preview")
    args = p.parse_args()
    prefix = Path(args.output).expanduser()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    specs = (("Namespaces in one pod", args.same_pod, (.035, .075, .065), (180, 110, 150)),
             ("Pods on the same node", args.same_node, (.18, .23, .21), (90, 65, 78)),
             ("Pods on different nodes", args.different_nodes, None, None))
    datasets, provenance = [], []
    try:
        for index, (name, folder, rtt, pps) in enumerate(specs):
            simulated = folder is None
            if simulated:
                if not args.simulate:
                    raise ValueError(f"Missing {name}; use --simulate to create a preview")
                # Independent seed per scenario: filling another panel does not change this one.
                rng = np.random.default_rng(np.random.SeedSequence([args.seed, index]))
                values = {kind: {tech: rng.normal(mean, mean*.035, 10).tolist()
                                 for tech, mean in zip(TECHS, means)}
                          for kind, means in (("ping", rtt), ("pps", pps))}
                details = dict(seed=args.seed, scenario_index=index, illustrative_only=True)
            else:
                values, inputs, snapshots = measured(folder)
                details = dict(inputs=inputs)
                for kind, raw in snapshots.items():
                    Path(str(prefix)+f"-input-{index}-{kind}.csv").write_bytes(raw)
            datasets.append((name, simulated, values))
            provenance.append(dict(scenario=name, simulated=simulated, samples=values, **details))
    except (OSError, ValueError, KeyError) as exc:
        p.error(str(exc))

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "pdf.fonttype": 42, "svg.fonttype": "none", "hatch.linewidth": .65,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 3, figsize=(11, 4.5))
    fig.subplots_adjust(left=.075, right=.985, top=.82, bottom=.10, wspace=.40, hspace=.35)
    records = []
    for col, (scenario, simulated, values) in enumerate(datasets):
        for row, (kind, field, divisor, ylabel) in enumerate(METRICS):
            ax = axes[row, col]
            high, low = 0, 0
            for index, tech in enumerate(TECHS):
                samples = np.asarray(values[kind][tech])
                n = len(samples)
                if n == 0:
                    p.error(f"No valid {scenario}/{kind}/{tech} measurements")
                mean = float(samples.mean())
                sd = float(samples.std(ddof=1)) if n > 1 else None
                half = float(t.ppf(.975, n-1)*sd/np.sqrt(n)) if n > 1 else None
                ax.bar(index, mean, width=.64, yerr=half, capsize=3,
                       color=COLORS[index], hatch=HATCHES[index], edgecolor="#333333",
                       linewidth=.7, error_kw={"elinewidth": 1}, zorder=3)
                text = f"{mean:.3f}" if kind == "ping" and mean < 1 else f"{mean:.2f}"
                ax.annotate(text, (index, mean+(half or 0)), xytext=(0, 4),
                            textcoords="offset points", ha="center", fontsize=8)
                high = max(high, mean+(half or 0))
                low = min(low, mean-(half or 0))
                records.append(dict(scenario=scenario, provenance="SIMULATED" if simulated else "MEASURED",
                                    metric=field, unit="ms" if kind == "ping" else "kpacket/s", technology=tech,
                                    n=n, mean=mean, sd=sd, ci95_low=mean-half if half is not None else None,
                                    ci95_high=mean+half if half is not None else None))
                if n < 2:
                    print(f"{scenario}/{kind}/{tech}: n={n}; no CI estimated", file=sys.stderr)
            ax.set_ylim(low*1.1, max(high*1.3, .001))
            ax.set_xticks(range(3), ("Baseline", "VXLAN", "L2TP") if row == 1 else ("", "", ""))
            ax.set_ylabel(ylabel)
            ax.grid(axis="y", alpha=.2, zorder=0)
            if row == 0:
                ax.set_title(scenario+("\n[SIMULATED — illustrative]" if simulated else "\n[MEASURED]"),
                             fontsize=9, pad=9)
    fig.legend(handles=[Patch(facecolor=c, edgecolor="#333333", hatch=h, label=label)
                        for c, h, label in zip(COLORS, HATCHES, ("Baseline/no-tunnel", "VXLAN", "L2TP"))],
               loc="upper center", bbox_to_anchor=(.5, 1.005), ncol=3, frameon=False, fontsize=9)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(str(prefix)+"."+ext, dpi=300)
    plt.close(fig)
    with Path(str(prefix)+"-statistics.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    metadata = dict(scenarios=provenance,
                    method="Run-level means; Student-t 95% CI only with n>=2. Synthetic CIs have no empirical interpretation.",
                    axes="Independent linear y-axis scales per panel; all RTT in ms, all PPS in kpacket/s",
                    interpretation="PPS is approximate receiver-interface arrival rate, not application delivery or loss-free forwarding capacity.")
    Path(str(prefix)+"-metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    print(f"Saved {prefix}.png / .pdf / .svg and statistics / provenance")


if __name__ == "__main__":
    main()
