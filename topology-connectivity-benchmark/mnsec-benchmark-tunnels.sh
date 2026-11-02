#!/usr/bin/env bash
# Run inside the pod after creating a namespace pair and its three links.
# Usage: bash benchmark-tunnels.sh [prefix] [rounds]
# Legacy usage: bash benchmark-tunnels.sh [rounds] (unprefixed lab-a/lab-b).
# Requires bash, iproute2, iperf3, jq; an iperf3 server in lab-b.
set -euo pipefail

# Console timestamps use the environment's local timezone (override with TZ).
# CSV timestamps remain explicitly UTC.
log() { printf '%s %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*"; }

usage() { echo "Usage: $0 [prefix] [rounds] (or: $0 [rounds])" >&2; }
(( $# <= 2 )) || { usage; exit 2; }
prefix=
if (( $# == 2 )); then
  prefix=$1
  N=$2
elif (( $# == 1 )) && [[ $1 =~ ^[0-9]+$ ]]; then
  N=$1
else
  prefix=${1:-}
  N=10
fi
if [[ -n $prefix && ! $prefix =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
  echo 'Prefix must start with a letter or digit and contain only letters, digits, _ or -.' >&2
  exit 2
fi
ns_a=${prefix:+$prefix-}labA
ns_b=${prefix:+$prefix-}labB
DURATION=${DURATION:-30}
OMIT=${OMIT:-3}
COOLDOWN=${COOLDOWN:-3}
MSS=${MSS:-1300}
CCA=${CCA:-cubic}
AFFINITY=${AFFINITY:-} # Optional client,server CPU numbers, e.g. 2,4.
OUT=${OUT:-tunnel-results-${prefix:+$prefix-}$(date -u +%Y%m%dT%H%M%SZ)-$$}

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
mnsecx "$ns_a" true
mnsecx "$ns_b" true
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
  printf 'PREFIX=%s NS_A=%s NS_B=%s\n' "$prefix" "$ns_a" "$ns_b"
  cat /proc/self/cgroup 2>/dev/null || true
  cg_path=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup 2>/dev/null || true)
  if [[ -n $cg_path ]]; then
    for f in cpu.max cpu.stat cpuset.cpus.effective; do
      cat "/sys/fs/cgroup${cg_path}/$f" 2>/dev/null || true
    done
  fi
  for ns in "$ns_a" "$ns_b"; do
    echo "=== $ns ==="
    mnsecx "$ns" ip -d link show
    mnsecx "$ns" ip address show
    mnsecx "$ns" ip route show
    mnsecx "$ns" sysctl net.ipv4.tcp_available_congestion_control
    mnsecx "$ns" tc -s qdisc show
    if command -v ethtool >/dev/null; then
      for dev in labA-eth0 labB-eth0 vxlan100 l2tpeth0; do
        mnsecx "$ns" ethtool -k "$dev" 2>/dev/null || true
      done
    fi
  done
} > "$OUT/configuration.txt" 2>&1

common=(-t "$DURATION" -O "$OMIT" -M "$MSS" -C "$CCA" -P 1 -J)
[[ -z $AFFINITY ]] || common+=(-A "$AFFINITY")
names=(veth vxlan l2tpv3)
sources=(192.0.2.1 198.51.100.1 203.0.113.1)
targets=(192.0.2.2 198.51.100.2 203.0.113.2)
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
    log "[$ns_a -> $ns_b] Round $round/$N, position $position: $name"
    cmd=(mnsecx "$ns_a" iperf3 -c "${targets[$index]}" -B "${sources[$index]}" "${common[@]}")
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
