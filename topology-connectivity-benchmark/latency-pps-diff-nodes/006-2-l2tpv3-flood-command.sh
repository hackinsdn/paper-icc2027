set -u
stats0=$(ip -j -s link show dev l2tpeth0)
tx0=$(printf '%s' "$stats0" | jq -r '.[0].stats64.tx.packets')
drop0=$(printf '%s' "$stats0" | jq -r '.[0].stats64.tx.dropped')
start=$(date +%s%N)
timeout --signal=INT --kill-after=2s 20s taskset -c 2 hping3 --numeric --udp --flood --spoof 203.0.113.1 --destport 49000 --baseport 49001 --keep --data 18 --interface l2tpeth0 203.0.113.2
rc=$?
end=$(date +%s%N)
stats1=$(ip -j -s link show dev l2tpeth0)
tx1=$(printf '%s' "$stats1" | jq -r '.[0].stats64.tx.packets')
drop1=$(printf '%s' "$stats1" | jq -r '.[0].stats64.tx.dropped')
printf '\nBENCH_COUNTERS %s %s %s %s %s %s\n' "$start" "$end" "$tx0" "$tx1" "$drop0" "$drop1"
exit "$rc"
