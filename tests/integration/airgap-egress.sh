#!/usr/bin/env bash
# Simulate a real air gap on lab nodes: block every outbound connection that leaves the allowed networks, LOG each attempt.
# Not persistent (a reboot or 'off' removes it). Run ON the node as root:
#   sudo bash airgap-egress.sh on  "172.27.15.0/24 10.200.0.0/24 10.201.0.0/24 10.1.0.0/16"
#   sudo bash airgap-egress.sh status        # rule counters + number of blocked attempts logged
#   sudo bash airgap-egress.sh off
# Applies to the node itself (OUTPUT) and to traffic it routes for system VMs / guests (FORWARD, routed only).
set -Eeuo pipefail
C=AIRGAP-EGRESS
case "${1:-}" in
on)
  ALLOW="${2:?allowed networks, e.g. \"172.27.15.0/24 10.200.0.0/23\"}"
  iptables -N $C 2>/dev/null || iptables -F $C
  iptables -A $C -o lo -j RETURN
  iptables -A $C -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  for n in $ALLOW 169.254.0.0/16 127.0.0.0/8 224.0.0.0/4; do iptables -A $C -d "$n" -j RETURN; done
  iptables -A $C -m physdev --physdev-is-bridged -j RETURN 2>/dev/null || true   # bridged frames: judged where they are routed
  iptables -A $C -m limit --limit 30/min -j LOG --log-prefix "AIRGAP-BLOCK " --log-level 4
  iptables -A $C -j REJECT --reject-with icmp-net-prohibited
  for ch in OUTPUT FORWARD; do iptables -C $ch -j $C 2>/dev/null || iptables -I $ch 1 -j $C; done
  echo "air gap ON - allowed: $ALLOW";;
off)
  for ch in OUTPUT FORWARD; do while iptables -D $ch -j $C 2>/dev/null; do :; done; done
  iptables -F $C 2>/dev/null || true; iptables -X $C 2>/dev/null || true
  echo "air gap OFF";;
status)
  if iptables -L $C -n -v 2>/dev/null | tail -n +3; then
    echo "blocked attempts logged: $(journalctl -k --since '-24h' --no-pager 2>/dev/null | grep -c 'AIRGAP-BLOCK')"
    journalctl -k --since '-24h' --no-pager 2>/dev/null | grep 'AIRGAP-BLOCK' | grep -oE 'DST=[0-9.]+ .*DPT=[0-9]+' | sed -E 's/ LEN.*PROTO=/ /' | sort | uniq -c | sort -rn | head -10
  else echo "air gap is OFF"; fi;;
*) echo "usage: $0 on \"<allowed cidrs>\" | off | status"; exit 1;;
esac
