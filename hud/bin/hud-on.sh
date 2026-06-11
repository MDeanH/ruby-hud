#!/usr/bin/env bash
# Switch the display to the graphical HUD. Root.
set -u

systemctl stop rubydash.service
systemctl start rubyhud.service

echo HUD_ON
