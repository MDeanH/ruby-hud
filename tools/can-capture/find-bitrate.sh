#!/usr/bin/env bash
# Probe a CAN segment's bitrate at the harness. The roof segment is most likely
# 125k MS-CAN; the dash bus is 500k HS-CAN. The correct rate yields many clean
# frames; a wrong rate yields ~0 frames and/or climbing bus-errors. Listen-only
# throughout -- never transmits.
#
#   sudo ./find-bitrate.sh <iface> [rate1 rate2 ...]
#   sudo ./find-bitrate.sh can1                 # tries 125k,500k,250k,1M
set -uo pipefail

IF="${1:?usage: find-bitrate.sh <iface> [rates...]}"; shift || true
RATES=("$@"); [ ${#RATES[@]} -gt 0 ] || RATES=(125000 500000 250000 1000000)

echo "Probing $IF (listen-only). The right rate = many frames, ~0 bus-errors."
for R in "${RATES[@]}"; do
    ip link set "$IF" down 2>/dev/null || true
    if ! ip link set "$IF" type can bitrate "$R" listen-only on 2>/dev/null; then
        printf "%8s bps : (rejected by driver)\n" "$R"; continue
    fi
    ip link set "$IF" up 2>/dev/null || { printf "%8s bps : (link up failed)\n" "$R"; continue; }
    sleep 0.3
    N=$(timeout 2 candump -n 80 "$IF" 2>/dev/null | wc -l | tr -d ' ')
    ERR=$(ip -details -statistics link show "$IF" 2>/dev/null | awk '/bus-error/{getline; print $1; exit}')
    printf "%8s bps : %3s frames / 2s   bus-errors=%s\n" "$R" "$N" "${ERR:-?}"
    ip link set "$IF" down 2>/dev/null || true
done
echo "Then: sudo ./setup-buses.sh $IF <the-good-rate>"
