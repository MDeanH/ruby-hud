#!/usr/bin/env bash
# setup-vision.sh -- idempotent root setup for the rubyvision service.
#
# Installs the system camera/CV packages from apt (NOT into the venv), creates
# the michael-owned vision data dirs, and makes the rubyvision package
# importable by the ruby-env interpreter (editable install, or a .pth fallback
# if pip cannot do the editable install).
#
# numpy + opencv + picamera2 come from apt / system-site-packages -- we never
# pip-install them into ruby-env (they need system libs and ABI matching).
#
# Run as root:  sudo bash deploy/setup-vision.sh
set -euo pipefail

MICHAEL_HOME="/home/michael"
VENV="${MICHAEL_HOME}/ruby-env"
VISION_DATA="${MICHAEL_HOME}/vision"

# Resolve the vision package from THIS script's own release, not a fixed path.
# ruby-updated runs deploy/setup.sh (which invokes us) from the staged release
# worktree, so $HERE is <release>/deploy and <release>/vision is the live code
# for this release. A hardcoded /home/michael/hud-repo/vision would install the
# stale one-time bootstrap clone (or fail if it was removed post-install).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Pin to the STABLE clone (/home/michael/hud-repo/vision), NOT a frozen A/B
# release worktree. The editable install writes an absolute import path; if it
# points at hud-releases/<tag>/vision the service silently runs stale vision
# code forever (this caused dets=0 despite HEF fixes — fixed 2026-06-12).
HUD_REPO_VISION="/home/michael/hud-repo/vision"
if [ -d "${HUD_REPO_VISION}" ]; then
    VISION_PKG="${HUD_REPO_VISION}"
else
    VISION_PKG="$(cd "${HERE}/../vision" 2>/dev/null && pwd || echo "${HERE}/../vision")"
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "setup-vision.sh must run as root (sudo)" >&2
    exit 1
fi

# --- apt packages (skip if already present) --------------------------------
APT_PKGS="python3-opencv python3-picamera2 python3-numpy"
MISSING=""
for pkg in ${APT_PKGS}; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
        MISSING="${MISSING} ${pkg}"
    fi
done
if [ -n "${MISSING}" ]; then
    echo "Installing apt packages:${MISSING}"
    apt-get update
    # shellcheck disable=SC2086
    apt-get install -y ${MISSING}
else
    echo "apt packages already present: ${APT_PKGS}"
fi

# --- data dirs (michael-owned) ---------------------------------------------
mkdir -p "${VISION_DATA}/models" "${VISION_DATA}/demo"
if id michael >/dev/null 2>&1; then
    chown -R michael:michael "${VISION_DATA}"
fi
echo "vision data dirs ready: ${VISION_DATA}/{models,demo}"

# --- make rubyvision importable by ruby-env --------------------------------
INSTALLED_OK=0
if [ -x "${VENV}/bin/python" ] && [ -d "${VISION_PKG}" ]; then
    # Preferred: editable install into the venv (no numpy/opencv pulled in;
    # rubyvision declares no hard deps).
    if "${VENV}/bin/python" -m pip install -e "${VISION_PKG}" \
            --no-build-isolation >/dev/null 2>&1; then
        echo "pip install -e ${VISION_PKG} OK"
        INSTALLED_OK=1
    else
        echo "editable install failed; falling back to a .pth entry" >&2
    fi

    if [ "${INSTALLED_OK}" -ne 1 ]; then
        # Fallback: drop a .pth into the venv site-packages pointing at the
        # package dir so 'import rubyvision' resolves.
        SITE_DIR="$("${VENV}/bin/python" -c \
            'import site,sys; print(next(iter(site.getsitepackages()), site.getusersitepackages()))' \
            2>/dev/null || true)"
        if [ -n "${SITE_DIR}" ] && [ -d "${SITE_DIR}" ]; then
            echo "${VISION_PKG}" > "${SITE_DIR}/rubyvision.pth"
            echo "wrote ${SITE_DIR}/rubyvision.pth -> ${VISION_PKG}"
            INSTALLED_OK=1
        else
            echo "could not locate venv site-packages for .pth fallback" >&2
        fi
    fi
else
    echo "venv ${VENV} or package ${VISION_PKG} missing; skipping install" >&2
fi

# --- verify import ----------------------------------------------------------
if [ "${INSTALLED_OK}" -eq 1 ] && [ -x "${VENV}/bin/python" ]; then
    if "${VENV}/bin/python" -c "import rubyvision" >/dev/null 2>&1; then
        echo "import rubyvision OK"
    else
        echo "WARNING: 'import rubyvision' still failing" >&2
    fi
fi

echo "VISION_SETUP_OK"
