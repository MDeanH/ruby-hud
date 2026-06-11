#!/usr/bin/env bash
# Per-release dependency setup for the ruby HUD. Root, idempotent.
# Run by ruby-updated during apply (timeout 300); safe to re-run by hand.
# Never upgrades or reinstalls anything already present, so it completes
# offline when all deps are in place.
set -u

VENV=/home/michael/ruby-env
PIP=$VENV/bin/pip
FONT_DST=/usr/share/fonts/truetype/rubyhud
APT_PKGS=(python3-evdev fonts-dejavu-core fbcat)
PIP_PKGS=(pillow numpy python-can)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "SETUP_FAIL: must run as root" >&2
    exit 1
fi

# 1. apt packages (dpkg -s check first; no apt call when all present).
missing=()
for pkg in "${APT_PKGS[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "setup: installing apt packages: ${missing[*]}"
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"; then
        echo "SETUP_FAIL: apt-get install ${missing[*]}" >&2
        exit 1
    fi
fi

# 2. Fonts shipped with the release (optional deploy/fonts/).
if [ -d "$HERE/fonts" ]; then
    mkdir -p "$FONT_DST"
    fonts_changed=0
    for f in "$HERE"/fonts/*; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        if [ ! -f "$FONT_DST/$base" ] || ! cmp -s "$f" "$FONT_DST/$base"; then
            cp "$f" "$FONT_DST/$base"
            fonts_changed=1
        fi
    done
    if [ "$fonts_changed" -eq 1 ] && command -v fc-cache >/dev/null 2>&1; then
        fc-cache -f "$FONT_DST" >/dev/null 2>&1
    fi
fi

# 3. Python deps in the HUD venv (pip show check; NEVER upgrade if present).
if [ ! -x "$PIP" ]; then
    echo "SETUP_FAIL: missing venv pip at $PIP" >&2
    exit 1
fi
for pkg in "${PIP_PKGS[@]}"; do
    if runuser -u michael -- "$PIP" show "$pkg" >/dev/null 2>&1; then
        continue
    fi
    echo "setup: pip install $pkg"
    if ! runuser -u michael -- "$PIP" install "$pkg"; then
        echo "SETUP_FAIL: pip install $pkg" >&2
        exit 1
    fi
done

# 4. rubyvision service deps: apt CV packages, data dirs, editable install into
#    ruby-env. Run from this same release ($HERE), so it installs THIS release's
#    vision package (release-relative, see setup-vision.sh). Idempotent.
if [ -f "$HERE/setup-vision.sh" ]; then
    echo "setup: running setup-vision.sh"
    if ! bash "$HERE/setup-vision.sh"; then
        echo "SETUP_FAIL: setup-vision.sh" >&2
        exit 1
    fi
fi

echo SETUP_OK
