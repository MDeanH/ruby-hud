#!/usr/bin/env bash
# One-shot fix for check-for-updates queue not re-triggering.
# Run ON THE PI (or via: ssh michael@<pi> 'sudo bash -s' < deploy/fix-updater-queue.sh)
set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -o root -g root -m 0755 "$HERE/ruby-updated" /usr/local/sbin/ruby-updated
install -o root -g root -m 0644 "$HERE/ruby-updated.path" /etc/systemd/system/ruby-updated.path
install -o root -g root -m 0644 "$HERE/ruby-updated.service" /etc/systemd/system/ruby-updated.service
systemd-tmpfiles --create "$HERE/tmpfiles-ruby-update.conf" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now ruby-updated.path
systemctl restart ruby-updated.path
echo "OK: ruby-updated.path restarted"
if id michael >/dev/null 2>&1 && [ -x /home/michael/hud/bin/ruby-update.sh ]; then
    runuser -u michael -- /home/michael/hud/bin/ruby-update.sh check
else
    echo "Run manually: ruby-update.sh check"
fi
