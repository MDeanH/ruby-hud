#!/usr/bin/env bash
# Stop the SIM driver and tear down vcan0. Root, idempotent.
set -u

systemctl stop rubyhud-sim 2>/dev/null
ip link del vcan0 2>/dev/null

echo SIM_DOWN
