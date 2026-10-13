#!/usr/bin/env bash
# Create and bring up a virtual SocketCAN interface. Usage: sudo scripts/setup_vcan.sh [vcan0]
set -euo pipefail
IFACE="${1:-vcan0}"
modprobe vcan || { echo "vcan kernel module not available (container/WSL1/macOS?)" >&2; exit 1; }
ip link show "$IFACE" >/dev/null 2>&1 || ip link add dev "$IFACE" type vcan
ip link set up "$IFACE"
echo "$IFACE is up. Try: candump $IFACE"
