#!/usr/bin/env bash
# Bring up vcan0 and start the SIM driver. Root, idempotent.
set -u

# 1. Ensure the vcan module and interface exist.
modprobe vcan
ip link add dev vcan0 type vcan 2>/dev/null
ip link set up vcan0

# 2. Start simdrive under systemd-run, unless already active.
if systemctl is-active --quiet rubyhud-sim; then
    echo SIM_UP
    exit 0
fi

systemd-run --unit=rubyhud-sim --collect \
    --working-directory=/home/michael/hud \
    /home/michael/ruby-env/bin/python -m rubyhud.simdrive

echo SIM_UP
