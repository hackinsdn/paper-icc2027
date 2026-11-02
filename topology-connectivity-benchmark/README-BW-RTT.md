# Throughput and RTT benchmark workflow

This workflow measures baseline/no-tunnel, VXLAN, and L2TP in three scenarios:

1. Two network namespaces in one pod.
2. Two pods on the same node.
3. Two pods on different nodes.

Run the benchmarks to generate CSV summaries, copy throughput results to your computer, regenerate plot metadata, and then generate the combined throughput + RTT figure. The final figure excludes PPS.

## 1. Prepare the environment

On your computer, open the project folder:

```bash
cd "/Users/italo/Documents/ChatGPT/HackInSDN Publication"
```

The local benchmark driver requires Python 3 and working `kubectl` access to your namespace. Plotting additionally requires:

```bash
python3 -m pip install matplotlib numpy scipy
```

Create the resources:

```
python3 -m pip install kubernp
python3
>>> from kubernp import KubeRNP
kubernp = KubeRNP()
exp = kubernp.create_experiment()
exp.create_from_file("mnsec-same-pod.yaml")
exp.list_resources()
exp2 = kubernp.create_experiment()
exp2.create_from_file("mnsec-same-node.yaml")
exp2.list_resources()
exp3 = kubernp.create_experiment()
exp3.create_from_file("mnsec-diff-nodes.yaml")
exp3.list_resources()
```

Use a virtual environment if required by your Python installation.

These setup scripts require root in privileged pods with the host's `/lib/modules` mounted. Use the existing lab setup before proceeding; do not recreate it while benchmarks are running.

Check pod placement on your computer:

```bash
kubectl get pods -o wide
```

For the two-pod scenarios, placement determines whether you are measuring the same node or different nodes. Replace pod names and IP addresses in the examples with your actual endpoints.

## 2. Run throughput benchmarks

The shell benchmark scripts run **inside the sender pod**, require Bash, iproute2, iperf3 and jq, and expect the receiver's iperf3 server to be running. Copy the appropriate script into the sender first.

### Two namespaces in one pod

On your computer:

```bash
kubectl cp mnsec-benchmark-tunnels.sh \
  mnsec-tunnel-exp-22bcf38e7f-bfc58f9d4-tkfq5:/tmp/benchmark-tunnels.sh
```

Run from your computer; the benchmark executes inside the pod:

```bash
kubectl exec mnsec-tunnel-exp-22bcf38e7f-bfc58f9d4-tkfq5 -- \
  env OUT=/tmp/throughput-same-pod-new \
  bash /tmp/benchmark-tunnels.sh 10
```

Copy the complete results folder back:

```bash
kubectl cp \
  mnsec-tunnel-exp-22bcf38e7f-bfc58f9d4-tkfq5:/tmp/throughput-same-pod-new \
  "$HOME/Downloads/throughput-same-pod-new"
```

### Two pods on the same node or different nodes

On your computer:

```bash
kubectl cp benchmark-pod-tunnels.sh \
  mnsec-laba-22bcf38e7f:/tmp/benchmark-pod-tunnels.sh

IP_POD_A=$(kubectl  get pod mnsec-laba-22bcf38e7f -o jsonpath='{.status.podIP}')
IP_POD_B=$(kubectl  get pod mnsec-labb-22bcf38e7f -o jsonpath='{.status.podIP}')

kubectl exec mnsec-laba-22bcf38e7f -- \
  env IP_POD_A="$IP_POD_A" IP_POD_B="$IP_POD_B" \
  OUT=/tmp/throughput-same-node-new \
  bash /tmp/benchmark-pod-tunnels.sh 10

kubectl cp \
  mnsec-laba-22bcf38e7f:/tmp/throughput-same-node-new \
  "$HOME/Downloads/throughput-same-node-new"
```

Repeat with pods placed on different nodes, using `throughput-different-nodes-new` as the output folder name in both commands. Finish collecting results before changing pod placement or rebuilding tunnels.

### Throughput settings and CSV output

The positional argument `10` sets repetitions per technology (also the default). The scripts rotate technology order across rounds. Defaults are 30 seconds per measured run, 3 omitted warm-up seconds, MSS 1300, one TCP flow, and CUBIC. Optional environment variables include `DURATION`, `OMIT`, `COOLDOWN`, `MSS`, `CCA`, and `AFFINITY` (client,server CPU numbers).

For example, include these in the `env` command when needed:

```text
DURATION=30 OMIT=3 MSS=1300 CCA=cubic AFFINITY=2,4
```

Choose CPU IDs allowed in the pods and keep settings consistent across scenarios.

Each run directory contains:

- `summary.csv`: per-run throughput, retransmissions, CPU measurements, and status.
- Per-run iperf3 JSON, command, and stderr files.
- A configuration snapshot and a copy of the benchmark script.

`summary.csv` is generated automatically during the benchmark; there is no separate summary-generation command. Use a new `OUT` directory for each collection: the script refuses an existing directory. Preserve raw logs alongside the summaries.

## 3. Run RTT and PPS benchmarks

Run `benchmark-latency-pps.py` **on your computer**, not inside a pod. It uses kubectl to execute measurements remotely and writes results locally.

The sender pod requires `bash`, `ping`, `hping3`, `timeout`, `date`, `ip`, `jq`, and standard shell utilities. Native-source mode also requires `iptables` and permission to manage rules in the receiving lab namespaces. For Debian/Ubuntu lab pods, install the tools before benchmarking:

```bash
kubectl  exec mnsec-laba-22bcf38e7f -- apt-get update
kubectl  exec mnsec-laba-22bcf38e7f -- apt-get install -y \
  hping3 iputils-ping iproute2 coreutils util-linux jq iptables

kubectl exec mnsec-labb-22bcf38e7f -- apt-get update
kubectl exec mnsec-labb-22bcf38e7f -- apt-get install -y iptables
```

For the same-pod scenario, install the sender tools and iptables in `test-deb-ids-es` instead.

### Two namespaces in one pod

```bash
python3 benchmark-latency-pps.py \
  --mode same-pod \
  --pod-a mnsec-tunnel-exp-22bcf38e7f-bfc58f9d4-tkfq5 \
  --source-mode native \
  --ping-count 100 --ping-interval 0.1 \
  --flood-seconds 20 \
  -n 10 \
  --output "./latency-pps-same-pod"
```

### Two pods on the same node

```bash
python3 benchmark-latency-pps.py \
  --mode pods \
  --pod-a mnsec-laba-22bcf38e7f --pod-b mnsec-labb-22bcf38e7f \
  --source-mode native \
  --ping-count 100 --ping-interval 0.1 \
  --flood-seconds 20 \
  -n 10 \
  --output "./latency-pps-same-node"
```

### Two pods on different nodes

Use the same command with your cross-node pod pair and change the output to:

```text
python3 benchmark-latency-pps.py \
  --mode pods \
  --pod-a mnsec-laba-1bcf3asdas --pod-b mnsec-labb-1bcf3asdas \
  --source-mode native \
  --ping-count 100 --ping-interval 0.1 \
  --flood-seconds 20 \
  -n 10 \
  --output "./latency-pps-diff-nodes"
```

The driver discovers pod IPs automatically. Exported `IP_POD_A` and `IP_POD_B` override discovery; unset stale overrides before using a different pod pair:

```bash
unset IP_POD_A IP_POD_B
```

### Latency/PPS settings and CSV output

Each technology receives 100 idle ping probes at 100 ms intervals per round, followed by a UDP generation test. Do not run throughput benchmarks concurrently with idle RTT measurements.

The output folder is created locally and contains:

- `ping-summary.csv`: per-run RTT statistics and status.
- `pps-summary.csv`: per-run aggregate interface PPS measurements and status.
- `senders-summary.csv`: per-worker generation measurements.
- Raw logs and configuration/provenance records.

These CSVs are written automatically during the benchmark. Use a new output folder for each collection; existing folders are refused. Do not use `--udp-only` when collecting RTT: it leaves `ping-summary.csv` with only a header.

The current latency metadata generator still reads both `ping-summary.csv` and `pps-summary.csv`, even though the final combined figure excludes PPS. The commands above therefore collect both. Native-source mode uses temporary, narrowly matched receiver firewall rules to suppress UDP replies; see `LATENCY-PPS.md` for details and multi-sender options. PPS measurements are interface arrivals rather than application delivery or a universal forwarding-capacity measurement.

## 4. Generate or update plot metadata

The following examples use the new folder names above. Replace them with the folders containing your actual results. No benchmark is executed by these plotting commands.

### Throughput metadata

Each input folder must contain `summary.csv`:

```bash
python3 plot-tunnel-scenarios.py \
  --same-pod "./throughput-same-pod" \
  --same-node "./throughput-same-node" \
  --different-nodes "./throughput-diff-nodes" \
  --compact \
  --output figures/tunnel-scenarios-final
```

This writes `figures/tunnel-scenarios-final-metadata.json`, a throughput figure, statistics, and provenance outputs.

### Latency metadata

Each input folder currently needs both `ping-summary.csv` and `pps-summary.csv`:

```bash
python3 plot-latency-pps-scenarios.py \
  --same-pod "./latency-pps-same-pod" \
  --same-node "./latency-pps-same-node" \
  --different-nodes "./latency-pps-diff-nodes" \
  --output figures/latency-pps-scenarios-preview
```

This writes `figures/latency-pps-scenarios-preview-metadata.json` and the separate latency/PPS figure and supporting outputs. The combined figure below reads only its RTT samples.

Rerun these commands after collecting new results. Keeping the same output prefixes replaces the previous plots and metadata; use a different prefix if you want to retain both versions. These scripts do not modify the source CSVs. Do not use `--simulate` for the paper's measured results.

## 4. Generate the combined throughput + RTT figure

With the metadata output prefixes above:

```bash
python3 plot-all-metrics.py
```

Or specify inputs and an output prefix explicitly:

```bash
python3 plot-all-metrics.py \
  --throughput figures/tunnel-scenarios-final-metadata.json \
  --latency-pps figures/latency-pps-scenarios-preview-metadata.json \
  --output figures/throughput-rtt
```

The explicit command generates:

```text
figures/throughput-rtt.png
figures/throughput-rtt.pdf
figures/throughput-rtt.svg
figures/throughput-rtt-statistics.csv
figures/throughput-rtt-metadata.json
```

Without `--output`, the prefix is `figures/all-metrics-scenarios-preview`, matching the current figure. Despite the legacy `--latency-pps` argument name, the combined figure displays only throughput and RTT.

The figure uses run means, hatches, and Student-t 95% confidence intervals when at least two valid runs are available. A single valid run has no confidence interval. Each subplot uses its own linear y-axis scale. Throughput and idle RTT are separate experiments.

## Updating an existing dataset

1. Run a fresh benchmark collection into a new folder; the summaries are created automatically.
2. Copy the complete throughput folder from the pod to your computer. Latency/PPS results are already local.
3. Point the scenario plotting scripts at the new folders to regenerate metadata.
4. Run `plot-all-metrics.py` to update the final figure.

The current benchmark scripts have no command-line option to rebuild CSV summaries from old raw logs. If a summary is missing but its raw logs remain, it needs a separate reconstruction step. Do not manually invent or edit measurement values to fill missing runs.
