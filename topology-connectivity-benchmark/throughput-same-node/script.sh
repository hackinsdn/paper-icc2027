#!/usr/bin/env bash
# Run in pod A after setup-pod-tunnels.sh has configured BOTH pods.
# Requires IP_POD_A and IP_POD_B. Baseline uses the existing CNI path.
set -euo pipefail

# Console timestamps use the environment's local timezone (override with TZ).
# CSV timestamps remain explicitly UTC.
log() { printf '%s %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*"; }

: "${IP_POD_A:?Set IP_POD_A}"
: "${IP_POD_B:?Set IP_POD_B}"
N=${1:-10}
DURATION=${DURATION:-30}
OMIT=${OMIT:-3}
COOLDOWN=${COOLDOWN:-3}
MSS=${MSS:-1300}
CCA=${CCA:-cubic}
AFFINITY=${AFFINITY:-} # Optional client,server CPU numbers, e.g. 2,4.
OUT=${OUT:-pod-tunnel-results-$(date -u +%Y%m%dT%H%M%SZ)-$$}

for value in "$N" "$DURATION" "$MSS"; do
  [[ $value =~ ^[0-9]+$ ]] && (( value > 0 )) || {
    log 'N, DURATION and MSS must be positive integers.' >&2; exit 2;
  }
done
for value in "$OMIT" "$COOLDOWN"; do
  [[ $value =~ ^[0-9]+$ ]] || {
    log 'OMIT and COOLDOWN must be nonnegative integers.' >&2; exit 2;
  }
done
for tool in ip iperf3 jq; do
  command -v "$tool" >/dev/null || { log "Missing tool: $tool" >&2; exit 2; }
done
ip netns exec pod-underlay true
ip netns exec pod-lab true
mkdir "$OUT"
OUT=$(cd "$OUT" && pwd)
cp -- "$0" "$OUT/script.sh"
printf '%s\n' 'round,position,technology,started_utc,status,receiver_bps,sender_bps,retransmits,sender_cpu_percent,receiver_cpu_percent,json_file' > "$OUT/summary.csv"

# Snapshot actual configuration, including offloads and queue disciplines.
{
  date -u
  uname -a
  iperf3 --version
  printf 'N=%s DURATION=%s OMIT=%s COOLDOWN=%s MSS=%s CCA=%s AFFINITY=%s\n' \
    "$N" "$DURATION" "$OMIT" "$COOLDOWN" "$MSS" "$CCA" "$AFFINITY"
  cat /proc/self/cgroup 2>/dev/null || true
  cg_path=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup 2>/dev/null || true)
  if [[ -n $cg_path ]]; then
    for f in cpu.max cpu.stat cpuset.cpus.effective; do
      cat "/sys/fs/cgroup${cg_path}/$f" 2>/dev/null || true
    done
  fi
  printf "IP_POD_A=%s IP_POD_B=%s\n" "$IP_POD_A" "$IP_POD_B"
  for ns in pod-underlay pod-lab; do
    echo "=== $ns ==="
    ip -n "$ns" -d link show
    ip -n "$ns" address show
    ip -n "$ns" route show
    ip netns exec "$ns" sysctl net.ipv4.tcp_available_congestion_control
    ip netns exec "$ns" tc -s qdisc show
    if command -v ethtool >/dev/null; then
      for dev in eth0 vxlan100 l2tpeth0; do
        ip netns exec "$ns" ethtool -k "$dev" 2>/dev/null || true
      done
    fi
  done
} > "$OUT/configuration.txt" 2>&1

common=(-t "$DURATION" -O "$OMIT" -M "$MSS" -C "$CCA" -P 1 -J)
[[ -z $AFFINITY ]] || common+=(-A "$AFFINITY")
names=(pod-cni vxlan l2tpv3)
client_ns=(pod-underlay pod-lab pod-lab)
sources=("$IP_POD_A" 198.51.100.1 203.0.113.1)
targets=("$IP_POD_B" 198.51.100.2 203.0.113.2)
failures=0

# Cycle through all six permutations to distribute test-order effects.
orders=('0 1 2' '1 2 0' '2 0 1' '2 1 0' '1 0 2' '0 2 1')
for ((round=1; round<=N; round++)); do
  read -r -a order <<< "${orders[$(((round-1)%6))]}"
  position=0
  for index in "${order[@]}"; do
    position=$((position+1))
    name=${names[$index]}
    stem=$(printf '%03d-%d-%s' "$round" "$position" "$name")
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    log "Round $round/$N, position $position: $name"
    cmd=(ip netns exec "${client_ns[$index]}" iperf3 -c "${targets[$index]}" -B "${sources[$index]}" "${common[@]}")
    printf '%q ' "${cmd[@]}" > "$OUT/$stem.command.txt"
    printf '\n' >> "$OUT/$stem.command.txt"
    rc=0
    "${cmd[@]}" > "$OUT/$stem.json" 2> "$OUT/$stem.stderr.txt" || rc=$?
    if (( rc == 0 )) && jq -e '
      (.error == null) and
      (.end.sum_received.bits_per_second | type == "number") and
      (.end.sum_sent.bits_per_second | type == "number")
    ' "$OUT/$stem.json" >/dev/null 2>&1; then
      jq -r --argjson round "$round" --argjson position "$position" \
        --arg name "$name" --arg started "$started" --arg file "$stem.json" '
        [$round, $position, $name, $started, "ok",
         .end.sum_received.bits_per_second, .end.sum_sent.bits_per_second,
         .end.sum_sent.retransmits,
         .end.cpu_utilization_percent.host_total,
         .end.cpu_utilization_percent.remote_total, $file] | @csv
      ' "$OUT/$stem.json" >> "$OUT/summary.csv"
    else
      failures=$((failures+1))
      printf '%s,%s,%s,%s,failed,,,,,,%s\n' \
        "$round" "$position" "$name" "$started" "$stem.json" >> "$OUT/summary.csv"
      log "FAILED: $stem (exit $rc); raw output retained." >&2
    fi
    sleep "$COOLDOWN"
  done
done
log "Results: $OUT"
log "Failed tests: $failures (not automatically retried or excluded)."
(( failures == 0 ))
