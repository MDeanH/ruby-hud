#!/usr/bin/env bash
# Capture a HUD frame to /tmp/hud.png. Root, idempotent.
# If the HUD is running, signal it (SIGUSR1) to dump the current frame.
# Otherwise render a one-shot frame against vcan0.
set -u

if pgrep -f 'python -m rubyhud$' >/dev/null 2>&1; then
    pkill -USR1 -f 'python -m rubyhud$'
else
    RUBYHUD_ONESHOT=1 RUBYHUD_CHANNEL=vcan0 \
        /home/michael/ruby-env/bin/python -m rubyhud
fi

echo SHOT_OK
