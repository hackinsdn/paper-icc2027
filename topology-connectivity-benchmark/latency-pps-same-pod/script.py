#!/usr/bin/env python3
"""Run from your computer: RTT and bounded hping3 UDP flood tests in lab pods.

Dependencies: local Python 3 + kubectl; pod A: ping, hping3, timeout, ip, date, jq.
Pod B: ip; native mode also requires iptables. Native mode adds temporary reply-
suppression rules in the receiver lab namespaces; no host/CNI settings change.
"""
import argparse
import csv
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid


def log(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("same-pod", "pods"), required=True)
    p.add_argument("--namespace", default="hackinsdn")
    p.add_argument("--pod-a", required=True)
    p.add_argument("--pod-b", help="Required in pods mode; omitted in same-pod mode")
    p.add_argument("-n", "--repetitions", type=int, default=10)
    p.add_argument("--ping-count", type=int, default=100)
    p.add_argument("--ping-interval", type=float, default=0.1)
    p.add_argument("--flood-seconds", type=int, default=10)
    p.add_argument("--payload-bytes", type=int, default=18, help="UDP payload; 18 gives 46-byte IPv4 packets")
    p.add_argument("--spoof-source", default="127.0.0.1")
    p.add_argument("--source-mode", choices=("spoof", "native"), default="spoof",
                   help="native uses each path's real source IP and temporarily drops matching UDP on receiver to suppress replies")
    p.add_argument("--udp-port", type=int, default=49000)
    p.add_argument("--cpu", type=int, help="Optional allowed CPU ID for the hping3 sender")
    p.add_argument("--cooldown", type=float, default=3)
    p.add_argument("--output", default="latency-pps-"+datetime.now().strftime("%Y%m%dT%H%M%S"))
    args = p.parse_args()
    if args.mode == "pods" and not args.pod_b:
        p.error("--pod-b is required in pods mode")
    if min(args.repetitions, args.ping_count, args.flood_seconds) < 1:
        p.error("Repetitions, ping count and flood seconds must be positive")
    if args.ping_interval <= 0 or args.cooldown < 0 or not 0 <= args.payload_bytes <= 1200:
        p.error("Require interval > 0, cooldown >= 0, payload 0..1200 (avoids lab fragmentation)")
    if not 1 <= args.udp_port <= 65535 or (args.cpu is not None and args.cpu < 0):
        p.error("Invalid port or CPU")
    try:
        ipaddress.IPv4Address(args.spoof_source)
    except ipaddress.AddressValueError as exc:
        p.error(str(exc))
    out = Path(args.output).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "script.py").write_bytes(Path(__file__).read_bytes())

    def kubectl(*command, timeout=30):
        return subprocess.run(["kubectl", "-n", args.namespace, *command],
                              text=True, capture_output=True, timeout=timeout)

    def execute(pod, ns, command, timeout=30):
        return kubectl("exec", pod, "--", "ip", "netns", "exec", ns,
                       *command, timeout=timeout)

    def checked(result):
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    try:
        pods = {}
        for pod in dict.fromkeys((args.pod_a, args.pod_b or args.pod_a)):
            data = json.loads(checked(kubectl("get", "pod", pod, "-o", "json")))
            pods[pod] = dict(node=data["spec"]["nodeName"], ip=data["status"]["podIP"],
                             containers=[dict(name=c["name"], image=c["image"], resources=c.get("resources", {}))
                                         for c in data["spec"]["containers"]],
                             image_ids=[c.get("imageID") for c in data["status"].get("containerStatuses", [])])
        if args.mode == "same-pod":
            receiver = args.pod_a
            paths = [("veth", "lab-a", "lab-b", "under-a", "under-b", "192.0.2.1", "192.0.2.2"),
                     ("vxlan", "lab-a", "lab-b", "vxlan100", "vxlan100", "198.51.100.1", "198.51.100.2"),
                     ("l2tpv3", "lab-a", "lab-b", "l2tpeth0", "l2tpeth0", "203.0.113.1", "203.0.113.2")]
        else:
            receiver = args.pod_b
            source = os.environ.get("IP_POD_A", pods[args.pod_a]["ip"])
            target = os.environ.get("IP_POD_B", pods[receiver]["ip"])
            for address in (source, target):
                ipaddress.IPv4Address(address)
            def device(pod, ns, destination):
                route = json.loads(checked(execute(pod, ns, ["ip", "-j", "route", "get", destination])))
                return route[0]["dev"]
            paths = [("pod-cni", "pod-underlay", "pod-underlay",
                      device(args.pod_a, "pod-underlay", target), device(receiver, "pod-underlay", source), source, target),
                     ("vxlan", "pod-lab", "pod-lab", "vxlan100", "vxlan100", "198.51.100.1", "198.51.100.2"),
                     ("l2tpv3", "pod-lab", "pod-lab", "l2tpeth0", "l2tpeth0", "203.0.113.1", "203.0.113.2")]
        for ns in sorted(set(path[1] for path in paths)):
            checked(execute(args.pod_a, ns, ["sh", "-c",
                'for tool in ping hping3 timeout date ip jq; do command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 127; }; done']))
        if args.source_mode == "native":
            for ns in sorted(set(path[2] for path in paths)):
                checked(execute(receiver, ns, ["iptables", "-w", "5", "-t", "raw", "-S", "PREROUTING"]))
        if args.cpu is not None:
            checked(execute(args.pod_a, paths[0][1], ["taskset", "-c", str(args.cpu), "true"]))
        for pod, namespaces in ((args.pod_a, set(x[1] for x in paths)), (receiver, set(x[2] for x in paths))):
            for ns in namespaces:
                result = execute(pod, ns, ["sh", "-c", "uname -a\nip -d link show\nip address show\nip route show\ntc -s qdisc show"])
                (out / f"configuration-{pod}-{ns}.txt").write_text(result.stdout+result.stderr)
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        p.error(f"Preflight failed: {exc}. Sender needs hping3 iputils-ping iproute2 coreutils util-linux jq; native mode also needs iptables and raw-table access in receiver namespaces.")

    metadata = dict(options=vars(args), pods=pods, paths=paths,
                    ping="Idle RTT; individual replies retained in raw ping logs; not measured during flood",
                    flood="Single hping3 process, UDP flood, fixed payload; source_mode selects spoofed or real per-path source; not a hardware forwarding-capacity test",
                    reply_suppression="In native mode only: temporary receiver raw PREROUTING DROP rule matching interface/source/destination/UDP port, removed in finally. Counts reflect arrival before this drop, not UDP application delivery.",
                    pps="Interface counter deltas / sender elapsed time. RX is approximate: before/after sampling includes kubectl latency and drain time, and counters include all interface traffic.",
                    loopback="127/8 may be filtered as martian/spoofed traffic. No route_localnet or CNI policy changes. Zero receiver packets is not a valid forwarding-capacity measurement.",
                    hping_loss="hping3 received/loss percentage is ignored: responses are not expected with a spoofed source")
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2)+"\n")
    ping_fields = ["round", "position", "technology", "started_utc", "status", "sent", "received", "loss_percent", "min_ms", "mean_ms", "max_ms", "mdev_ms"]
    flood_fields = ["round", "position", "technology", "started_utc", "status", "elapsed_seconds", "payload_bytes", "source_mode", "source_address", "spoof_source", "hping_reported_sent", "sender_interface_tx_packets", "receiver_interface_rx_packets", "sender_interface_pps", "receiver_interface_pps_approx", "sender_tx_drops", "receiver_rx_drops"]

    def snapshot(pod, ns, dev):
        text = checked(execute(pod, ns, ["ip", "-j", "-s", "link", "show", "dev", dev]))
        return json.loads(text)[0]["stats64"]

    failures = 0
    if args.mode == "pods" and args.source_mode == "spoof":
        log("WARNING: spoofed baseline traffic may be blocked by CNI source-IP validation; use --source-mode native for comparable arrival measurements.")
    orders = ((0, 1, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0), (1, 0, 2), (0, 2, 1))
    with (out / "ping-summary.csv").open("w", newline="") as pf, (out / "pps-summary.csv").open("w", newline="") as ff:
        pw, fw = csv.DictWriter(pf, fieldnames=ping_fields), csv.DictWriter(ff, fieldnames=flood_fields)
        pw.writeheader(); fw.writeheader()
        for round_number in range(1, args.repetitions+1):
            for position, index in enumerate(orders[(round_number-1)%6], 1):
                tech, src_ns, dst_ns, src_dev, dst_dev, source, target = paths[index]
                stem = f"{round_number:03d}-{position}-{tech}"
                common = dict(round=round_number, position=position, technology=tech)
                log(f"Round {round_number}/{args.repetitions}, position {position}: {tech}, idle ping")
                row = dict(common, started_utc=datetime.now(timezone.utc).isoformat(), status="failed")
                deadline = int(args.ping_count*args.ping_interval)+10
                command = ["ping", "-n", "-D", "-c", str(args.ping_count), "-i", str(args.ping_interval),
                           "-W", "2", "-w", str(deadline), "-I", source, target]
                result = execute(args.pod_a, src_ns, command, timeout=deadline+30)
                text = result.stdout+result.stderr
                (out / f"{stem}-ping.txt").write_text(text)
                (out / f"{stem}-ping-command.json").write_text(json.dumps(command)+"\n")
                counts = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received,.*?([\d.]+)% packet loss", text)
                timing = re.search(r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", text)
                if counts:
                    row.update(sent=int(counts[1]), received=int(counts[2]), loss_percent=float(counts[3]))
                if timing and result.returncode in (0, 1):
                    row.update(zip(("min_ms", "mean_ms", "max_ms", "mdev_ms"), map(float, timing.groups())))
                    row["status"] = "ok" if result.returncode == 0 else "partial"
                else:
                    failures += 1
                pw.writerow(row); pf.flush()
                time.sleep(args.cooldown)

                log(f"Round {round_number}/{args.repetitions}, position {position}: {tech}, UDP flood {args.flood_seconds}s")
                actual_source = source if args.source_mode == "native" else args.spoof_source
                row = dict(common, started_utc=datetime.now(timezone.utc).isoformat(), status="failed",
                           payload_bytes=args.payload_bytes, source_mode=args.source_mode,
                           source_address=actual_source,
                           spoof_source=args.spoof_source if args.source_mode == "spoof" else "")
                delete_rule = None
                try:
                    if args.source_mode == "native":
                        comment = "pps-bench-"+uuid.uuid4().hex[:16]
                        rule = ["-i", dst_dev, "-s", source, "-d", target, "-p", "udp",
                                "--dport", str(args.udp_port), "-m", "comment", "--comment", comment, "-j", "DROP"]
                        cleanup_command = ["iptables", "-w", "5", "-t", "raw", "-D", "PREROUTING", *rule]
                        (out / f"{stem}-receiver-rule.json").write_text(json.dumps(dict(
                            receiver=receiver, namespace=dst_ns, cleanup_command=cleanup_command), indent=2)+"\n")
                        checked(execute(receiver, dst_ns, ["iptables", "-w", "5", "-t", "raw", "-I", "PREROUTING", "1", *rule]))
                        delete_rule = cleanup_command
                    before = snapshot(receiver, dst_ns, dst_dev)
                    hping = ["hping3", "--numeric", "--udp", "--flood", "--spoof", actual_source,
                             "--destport", str(args.udp_port), "--baseport", "49001", "--keep",
                             "--data", str(args.payload_bytes), "--interface", src_dev, target]
                    if args.cpu is not None:
                        hping = ["taskset", "-c", str(args.cpu), *hping]
                    stats_command = shlex.join(["ip", "-j", "-s", "link", "show", "dev", src_dev])
                    shell = f"""set -u
stats0=$({stats_command})
tx0=$(printf '%s' "$stats0" | jq -r '.[0].stats64.tx.packets')
drop0=$(printf '%s' "$stats0" | jq -r '.[0].stats64.tx.dropped')
start=$(date +%s%N)
timeout --signal=INT --kill-after=2s {args.flood_seconds}s {shlex.join(hping)}
rc=$?
end=$(date +%s%N)
stats1=$({stats_command})
tx1=$(printf '%s' "$stats1" | jq -r '.[0].stats64.tx.packets')
drop1=$(printf '%s' "$stats1" | jq -r '.[0].stats64.tx.dropped')
printf '\\nBENCH_COUNTERS %s %s %s %s %s %s\\n' "$start" "$end" "$tx0" "$tx1" "$drop0" "$drop1"
exit "$rc"
"""
                    (out / f"{stem}-flood-command.sh").write_text(shell)
                    result = execute(args.pod_a, src_ns, ["sh", "-c", shell], timeout=args.flood_seconds+40)
                    # Allow in-flight packets to drain before the receiver snapshot.
                    time.sleep(0.25)
                    after = snapshot(receiver, dst_ns, dst_dev)
                    text = result.stdout+result.stderr
                    (out / f"{stem}-flood.txt").write_text(text)
                    (out / f"{stem}-receiver-counters.json").write_text(json.dumps(dict(before=before, after=after), indent=2)+"\n")
                    match = re.search(r"BENCH_COUNTERS (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)", text)
                    if not match or result.returncode not in (0, 124):
                        raise RuntimeError(f"Flood command exit {result.returncode}; see raw log")
                    start, end, tx0, tx1, drop0, drop1 = map(int, match.groups())
                    elapsed = (end-start)/1e9
                    tx, rx = tx1-tx0, after["rx"]["packets"]-before["rx"]["packets"]
                    if elapsed <= 0 or tx < 0 or rx < 0:
                        raise RuntimeError("Invalid counter/time delta")
                    sent = re.search(r"(\d+) packets (?:transmitted|tramitted)", text)
                    row.update(status="interface_activity_only" if tx > 0 and rx > 0 else "no_receiver_packets" if rx == 0 else "no_sender_packets",
                               elapsed_seconds=elapsed, hping_reported_sent=int(sent[1]) if sent else "",
                               sender_interface_tx_packets=tx, receiver_interface_rx_packets=rx,
                               sender_interface_pps=tx/elapsed, receiver_interface_pps_approx=rx/elapsed,
                               sender_tx_drops=drop1-drop0,
                               receiver_rx_drops=after["rx"]["dropped"]-before["rx"]["dropped"])
                    log(f"{tech}: TX {tx/elapsed:,.0f} interface PPS; RX ~{rx/elapsed:,.0f} interface PPS ({row['status']})")
                    if row["status"] != "interface_activity_only": failures += 1
                except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                    failures += 1
                    (out / f"{stem}-error.txt").write_text(str(exc)+"\n")
                    log(f"FAILED {tech}: {exc}")
                finally:
                    if delete_rule is not None:
                        try:
                            checked(execute(receiver, dst_ns, delete_rule))
                        except (RuntimeError, subprocess.TimeoutExpired) as exc:
                            failures += 1
                            row["status"] = "receiver_rule_cleanup_failed"
                            log(f"CLEANUP FAILED: {exc}; exact removal command saved in {stem}-receiver-rule.json")
                fw.writerow(row); ff.flush()
                time.sleep(args.cooldown)
    log(f"Results: {out}; failed/invalid tests: {failures}")
    return int(failures > 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted. Any active flood is bounded by its remote timeout.", file=sys.stderr)
        sys.exit(130)
