#!/usr/bin/env bash
# setup-sat.sh -- idempotent root setup for the rubysat service.
#
# Makes the rubysat AND rubyhud packages importable by the ruby-env interpreter
# (editable install, or a .pth fallback if pip cannot do the editable install).
# rubysat is stdlib-only -- no apt packages, no pip deps. The Pi has no firewall
# by default, so we open nothing: the TCP listener on 0.0.0.0:7878 is reachable
# as soon as the service starts.
#
# Why both packages: rubysat.service no longer puts anything on PYTHONPATH (the
# old PYTHONPATH=/home/michael/hud/../sat trick was broken -- CPython lexically
# normalizes "hud/.." to "/home/michael" BEFORE traversing the symlink, so it
# never resolved to the live release). Instead the unit relies entirely on these
# venv-level installs, exactly like rubyvision. rubyhud is pinned too so rubysat
# can import rubyhud.signals for live vehicle data.
#
# This script is invoked by deploy/setup.sh on EVERY OTA apply, from the staged
# release worktree, BEFORE the symlink flip. So ${HERE}/../sat and ${HERE}/../hud
# resolve to the about-to-be-live release's code, re-pinning both packages to the
# new release on every apply (a pruned old-release path can never linger).
#
# Run as root:  sudo bash deploy/setup-sat.sh
set -euo pipefail

MICHAEL_HOME="/home/michael"
VENV="${MICHAEL_HOME}/ruby-env"

# Resolve the sat + hud packages from THIS script's own release, not a fixed
# path. $HERE is <release>/deploy, so <release>/{sat,hud} is the live code for
# this release. A hardcoded path would install the stale bootstrap clone (or
# fail if it was removed post-install).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAT_PKG="$(cd "${HERE}/../sat" 2>/dev/null && pwd || echo "${HERE}/../sat")"
HUD_PKG="$(cd "${HERE}/../hud" 2>/dev/null && pwd || echo "${HERE}/../hud")"

if [ "$(id -u)" -ne 0 ]; then
    echo "setup-sat.sh must run as root (sudo)" >&2
    exit 1
fi

# pin_pkg <pkg-dir> <import-name> <pth-basename>: editable-install <pkg-dir>
# into the venv, or fall back to a .pth pointing at it. Echoes progress; sets
# global PINNED_OK=1 on success, 0 on failure (caller decides hardness).
PINNED_OK=0
pin_pkg() {
    local pkg_dir="$1" import_name="$2" pth_base="$3"
    PINNED_OK=0
    if [ ! -x "${VENV}/bin/python" ] || [ ! -d "${pkg_dir}" ]; then
        echo "venv ${VENV} or package ${pkg_dir} missing; skipping ${import_name}" >&2
        return 0
    fi
    # Preferred: editable install into the venv.
    if "${VENV}/bin/python" -m pip install -e "${pkg_dir}" \
            --no-build-isolation >/dev/null 2>&1; then
        echo "pip install -e ${pkg_dir} OK"
        PINNED_OK=1
        return 0
    fi
    echo "editable install of ${pkg_dir} failed; falling back to a .pth entry" >&2
    # Fallback: drop a .pth into the venv site-packages so the import resolves.
    local site_dir
    site_dir="$("${VENV}/bin/python" -c \
        'import site,sys; print(next(iter(site.getsitepackages()), site.getusersitepackages()))' \
        2>/dev/null || true)"
    if [ -n "${site_dir}" ] && [ -d "${site_dir}" ]; then
        echo "${pkg_dir}" > "${site_dir}/${pth_base}"
        echo "wrote ${site_dir}/${pth_base} -> ${pkg_dir}"
        PINNED_OK=1
    else
        echo "could not locate venv site-packages for .pth fallback" >&2
    fi
    return 0
}

# --- pin rubysat (required) and rubyhud (best-effort) ----------------------
pin_pkg "${SAT_PKG}" rubysat rubysat.pth
SAT_PINNED="${PINNED_OK}"

# rubyhud supplies live vehicle data; on a bench host it may be absent. Pin it
# if present, but a missing rubyhud must not fail the sat deploy (rubysat falls
# back to demo snapshots when rubyhud is not importable).
if [ -d "${HUD_PKG}" ]; then
    pin_pkg "${HUD_PKG}" rubyhud rubyhud.pth
fi

# --- verify import (HARD: a broken rubysat must FAIL the deploy) ------------
# This is the post-apply smoke check: if rubysat can't import after pinning,
# fail setup.sh so ruby-updated aborts the apply and leaves the live tree
# untouched -- far better than flipping the symlink and crash-looping the unit.
if [ "${SAT_PINNED}" -ne 1 ]; then
    echo "SAT_SETUP_FAIL: could not pin rubysat into ${VENV}" >&2
    exit 1
fi
if ! "${VENV}/bin/python" -c "import rubysat" >/dev/null 2>&1; then
    echo "SAT_SETUP_FAIL: 'import rubysat' still failing after pinning" >&2
    exit 1
fi
echo "import rubysat OK"

# No firewall on the Pi by default -- nothing to open for TCP 7878.
echo "SAT_SETUP_OK"
