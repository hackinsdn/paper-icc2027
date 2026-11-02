#!/usr/bin/env python3
"""Compact RTT / sender PPS / approximate receiver PPS figure from one results folder.
Each run is one replicate. Student-t 95% CIs are shown only with n >= 2.
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", help="Folder containing ping-summary.csv and pps-summary.csv")
    p.add_argument("--output", default="figures/latency-pps-different-nodes")
    args = p.parse_args()
    folder = Path(args.results).expanduser().resolve()
    prefix = Path(args.output).expanduser()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    inputs, tables, snapshots = [], {}, {}
    for kind in ("ping", "pps"):
        path = folder / f"{kind}-summary.csv"
        raw = path.read_bytes()
        # Parse precisely the bytes saved below, even if tests are still running.
        rows = list(csv.DictReader(io.StringIO(raw.decode())))
        seen = set()
        for row in rows:
            row["technology"] = {"veth": "baseline", "pod-cni": "baseline"}.get(row["technology"], row["technology"])
            key = (row["round"], row["technology"])
            if key in seen or row["technology"] not in TECHS:
                p.error(f"Duplicate or unknown technology in {path}: {key}")
            seen.add(key)
        tables[kind] = rows
        snapshots[kind] = raw
        inputs.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()))
    modes = {r.get("source_mode", "legacy-spoof") for r in tables["pps"]}
    if len(modes) > 1:
        p.error("PPS file mixes source modes; split into separate experiments")
    panels = [("ping", "mean_ms", 1, "Mean RTT (ms)", "Mean RTT"),
              ("pps", "sender_interface_pps", 1000, "Sender rate (kpacket/s)", "Sender PPS"),
              ("pps", "receiver_interface_pps_approx", 1000, "Receiver rate (kpacket/s)", "Receiver PPS (approx.)")]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "pdf.fonttype": 42, "svg.fonttype": "none", "hatch.linewidth": 0.65,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 3, figsize=(11, 2.8))
    fig.subplots_adjust(left=0.065, right=0.985, bottom=0.16, top=0.78, wspace=0.4)
    stats = []
    for panel, (ax, (kind, field, divisor, ylabel, title)) in enumerate(zip(axes, panels)):
        high = 0
        low = 0
        for index, tech in enumerate(TECHS):
            valid_status = {"ok", "partial"} if kind == "ping" else {"interface_activity_only"}
            rows = [r for r in tables[kind] if r["technology"] == tech and r["status"] in valid_status]
            values = np.asarray([float(r[field])/divisor for r in rows], dtype=float)
            if not len(values) or not np.all(np.isfinite(values)) or np.any(values < 0):
                p.error(f"No valid finite measurements for {tech}/{field}")
            n = len(values)
            mean = float(values.mean())
            sd = float(values.std(ddof=1)) if n >= 2 else None
            half = float(t.ppf(0.975, n-1)*sd/np.sqrt(n)) if n >= 2 else None
            ax.bar(index, mean, width=0.64, yerr=half, capsize=4,
                   color=COLORS[index], hatch=HATCHES[index], edgecolor="#333333",
                   linewidth=0.8, error_kw={"elinewidth": 1.1}, zorder=3)
            ax.annotate(f"{mean:.2f}", (index, mean+(half or 0)), xytext=(0, 6),
                        textcoords="offset points", ha="center", fontsize=9)
            high = max(high, mean+(half or 0))
            low = min(low, mean-(half or 0))
            stats.append(dict(metric=field, unit="ms" if kind == "ping" else "kpacket/s",
                              technology=tech, n=n, mean=mean, sd=sd,
                              ci95_low=mean-half if half is not None else None,
                              ci95_high=mean+half if half is not None else None))
            if n < 2:
                print(f"WARNING: {tech}/{field}: n={n}; no 95% CI estimated", file=sys.stderr)
        ax.set_ylim(low*1.1, max(high*1.22, 0.1))
        ax.set_xticks(range(3), ("Baseline", "VXLAN", "L2TP"))
        ax.set_ylabel(ylabel)
        ax.set_title(f"({chr(97+panel)}) {title}", fontsize=10, pad=8)
        ax.grid(axis="y", alpha=0.2, zorder=0)
    fig.legend(handles=[Patch(facecolor=c, edgecolor="#333333", hatch=h, label=label)
                        for c, h, label in zip(COLORS, HATCHES, ("Baseline/no-tunnel", "VXLAN", "L2TP"))],
               loc="upper center", bbox_to_anchor=(0.5, 1.02), ncol=3, frameon=False, fontsize=9)
    for extension in ("png", "pdf", "svg"):
        fig.savefig(str(prefix)+"."+extension, dpi=300)
    plt.close(fig)
    with Path(str(prefix)+"-statistics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(stats[0]))
        writer.writeheader(); writer.writerows(stats)
    for kind, raw in snapshots.items():
        Path(str(prefix)+f"-input-{kind}.csv").write_bytes(raw)
    metadata = dict(inputs=inputs, source_modes=sorted(modes),
                    method="Per-run means; two-sided Student-t 95% CI when n >= 2; otherwise CI unavailable, not zero.",
                    pps_interpretation="Maximum observed with one hping3 process. Interface counters include background traffic; receiver rate uses sender elapsed time and is approximate. Not loss-free forwarding capacity.",
                    excluded_rows={kind: [r for r in rows if r["status"] not in ({"ok", "partial"} if kind == "ping" else {"interface_activity_only"})] for kind, rows in tables.items()},
                    statistics=stats)
    source_metadata = folder / "metadata.json"
    if source_metadata.exists():
        metadata["benchmark_metadata"] = json.loads(source_metadata.read_text())
    Path(str(prefix)+"-metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    print(f"Saved {prefix}.png / .pdf / .svg and statistics / input snapshots / metadata")


if __name__ == "__main__":
    main()
