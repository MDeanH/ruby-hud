#!/usr/bin/env bash
# Switch the display back to rubydash. Root.
set -u

systemctl stop rubyhud.service
systemctl start rubydash.service

echo HUD_OFF
