#!/usr/bin/env bash
# One-time root bootstrap for the ruby HUD self-updater. Idempotent.
# Expects the repo already cloned at /home/michael/hud-repo, then:
#   sudo bash /home/michael/hud-repo/deploy/install.sh
# After this, updates flow through the queue (hud/bin/ruby-update.sh) and
# the handler keeps itself and the units in sync on each apply.
set -u

REPO=/home/michael/hud-repo
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

fail() {
    echo "INSTALL_FAIL: $*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "must run as root"
[ -e "$REPO/.git" ] || fail "no git clone at $REPO (clone it first)"

# 1. Privileged handler.
install -o root -g root -m 0755 "$HERE/ruby-updated" \
    /usr/local/sbin/ruby-updated || fail "install ruby-updated"

# 2. systemd units, rubyhud drop-in, tmpfiles.
install -o root -g root -m 0644 "$HERE/ruby-updated.path" \
    /etc/systemd/system/ruby-updated.path || fail "install ruby-updated.path"
install -o root -g root -m 0644 "$HERE/ruby-updated.service" \
    /etc/systemd/system/ruby-updated.service || fail "install ruby-updated.service"
install -o root -g root -m 0644 "$HERE/ruby-rollback.service" \
    /etc/systemd/system/ruby-rollback.service || fail "install ruby-rollback.service"
mkdir -p /etc/systemd/system/rubyhud.service.d
install -o root -g root -m 0644 "$HERE/rubyhud-health.conf" \
    /etc/systemd/system/rubyhud.service.d/10-health.conf || fail "install 10-health.conf"
install -o root -g root -m 0644 "$HERE/tmpfiles-ruby-update.conf" \
    /etc/tmpfiles.d/ruby-update.conf || fail "install tmpfiles conf"

# 3. Runtime + state directories.
systemd-tmpfiles --create /etc/tmpfiles.d/ruby-update.conf || fail "systemd-tmpfiles"
mkdir -p /home/michael/hud-state /home/michael/hud-releases
chown michael:michael /home/michael/hud-state /home/michael/hud-releases

# 4. Arm the queue watcher.
systemctl daemon-reload || fail "daemon-reload"
systemctl enable --now ruby-updated.path || fail "enable ruby-updated.path"

echo INSTALL_OK
