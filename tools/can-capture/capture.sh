#!/usr/bin/env bash
# Dual-bus timestamped capture for the roof reverse-engineering session. Logs the
# dash bus (for the 0x472 RoofGraphicStatus clock) AND the roof segment at the
# same time, so diff-frames.py can time-align the command against the animation.
# Both buses must already be up listen-only (see setup-buses.sh).
#
#   ./capture.sh <label> [dashbus] [roofbus]
#   ./capture.sh baseline   can0 can1     # ~60s untouched, then Ctrl-C
#   ./capture.sh roof-open  can0 can1     # while holding the OEM switch OPEN
#   ./capture.sh roof-close can0 can1     # while holding it CLOSE
set -uo pipefail

LABEL="${1:?usage: capture.sh <label> [dashbus] [roofbus]}"
DASH="${2:-can0}"; ROOF="${3:-can1}"
mkdir -p logs
TS=$(date +%Y%m%d-%H%M%S)
D="logs/${LABEL}-${DASH}-${TS}.log"
R="logs/${LABEL}-${ROOF}-${TS}.log"

echo "Capturing  $DASH -> $D   and   $ROOF -> $R"
echo "Work the switch now. Ctrl-C to stop."
candump -ta "$DASH" > "$D" &  P1=$!
candump -ta "$ROOF" > "$R" &  P2=$!
trap 'kill $P1 $P2 2>/dev/null' INT TERM
wait $P1 $P2 2>/dev/null
echo "Saved:"
wc -l "$D" "$R" 2>/dev/null
echo "Diff:  ./diff-frames.py logs/baseline-${ROOF}-*.log $R"
