#!/usr/bin/env bash
# Bring a CAN interface up in LISTEN-ONLY mode -- it physically cannot transmit.
# This is the hard guard for the whole roof/window capture: every interface used
# on the car must come up listen-only, verified, or we refuse to use it.
#
#   sudo ./setup-buses.sh <iface> <bitrate>
#   sudo ./setup-buses.sh can1 125000     # roof segment (likely 125k MS-CAN)
#   sudo ./setup-buses.sh can0 500000     # dash bus (500k HS-CAN, the 0x472 clock)
set -euo pipefail

IF="${1:?usage: setup-buses.sh <iface> <bitrate>}"
RATE="${2:?usage: setup-buses.sh <iface> <bitrate>}"

ip link set "$IF" down 2>/dev/null || true
ip link set "$IF" type can bitrate "$RATE" listen-only on
ip link set "$IF" up

if ip -details link show "$IF" | grep -qi "listen-only"; then
    echo "OK: $IF up at ${RATE} bps, LISTEN-ONLY confirmed (cannot transmit)."
    ip -details link show "$IF" | sed -n '1,3p'
else
    echo "REFUSING: $IF did NOT come up listen-only -- taking it back down." >&2
    ip link set "$IF" down || true
    exit 1
fi
