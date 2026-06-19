"""Persistent user config for rubyhud (display units + UI prefs).

Stored as a small JSON file under hud-state/ so it survives OTA release flips
(the release worktree is replaced, hud-state is not). Read on the render path
every frame, so loads are cached in-process; writes update the cache
immediately (settings.py runs inside the rubyhud process, so a toggle takes
effect on the very next frame). Everything is failure-guarded: a missing or
corrupt file falls back to defaults and never raises into the renderer.
"""

from __future__ import annotations

import json
import os
import threading
import time

_DEFAULTS = {
    "temp_unit": "F",      # "F" | "C"
    "speed_unit": "MPH",   # "MPH" | "KMH"
    "vision_source": "usb",  # "usb" | "csi" — AI-vision camera (rubyvision)
    "ai_vision": True,     # AI-vision page + menu present; set False on hardware
                           # without the Hailo accelerator (e.g. the Pi-4 unit)
    "shift_rpm": 6500,     # amber shift-light threshold on the 4" satellite
    "shift_enabled": True,  # whether the shift light fires at all
    "sat_mirror": False,   # 4" horizontal flip (for windshield-reflection HUD)
    "sat_rotate": False,   # 4" 180-degree rotation (inverted mount)
}

_PATH = os.environ.get(
    "RUBYHUD_CONFIG",
    os.path.join(os.path.expanduser("~"), "hud-state", "rubyhud-config.json"))

_lock = threading.Lock()
_cache: dict | None = None
_mtime: float = -1.0      # mtime of _PATH when _cache was last read
_checked: float = 0.0     # monotonic of our last stat (throttle)
_RELOAD_TTL = 0.5         # seconds between cross-process staleness checks


def _read_disk() -> dict:
    data = dict(_DEFAULTS)
    try:
        with open(_PATH) as fh:
            disk = json.load(fh)
        if isinstance(disk, dict):
            for k in _DEFAULTS:
                if k in disk:
                    data[k] = disk[k]
    except Exception:
        pass
    return data


def _load() -> dict:
    """Return the cached config, transparently reloading when another PROCESS
    changed the file on disk.

    This is essential now that the 4" satellite is rendered in a separate
    process (satdriver): a setting toggled on the 7" HUD (e.g. the shift-light
    threshold) writes the JSON here, and the satellite renderer must pick it up.
    The check is a throttled os.stat, so the render hot path pays at most one
    stat every _RELOAD_TTL seconds; within a process, writes still update the
    cache immediately. Never raises into the renderer."""
    global _cache, _mtime, _checked
    if _cache is None:
        _cache = _read_disk()
        try:
            _mtime = os.path.getmtime(_PATH)
        except OSError:
            _mtime = -1.0
        _checked = time.monotonic()
        return _cache
    now = time.monotonic()
    if now - _checked >= _RELOAD_TTL:
        _checked = now
        try:
            m = os.path.getmtime(_PATH)
        except OSError:
            m = -1.0
        if m != _mtime:
            _cache = _read_disk()
            _mtime = m
    return _cache


def get(key: str, default=None):
    with _lock:
        d = _load()
        if default is None:
            default = _DEFAULTS.get(key)
        return d.get(key, default)


def set(key: str, value) -> None:
    """Update one key and persist atomically. Never raises."""
    global _mtime
    with _lock:
        d = _load()
        d[key] = value
        try:
            os.makedirs(os.path.dirname(_PATH), exist_ok=True)
            tmp = _PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh)
            os.replace(tmp, _PATH)
            # Track our own write so this process doesn't re-read its own file;
            # other processes see a different mtime and reload (see _load).
            try:
                _mtime = os.path.getmtime(_PATH)
            except OSError:
                pass
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Units: internal state is always Celsius / mph; these convert at the display
# layer only (so the CAN decode and the STATE wire schema never change).
# --------------------------------------------------------------------------- #
def temp_unit() -> str:
    return get("temp_unit", "F")


def speed_unit() -> str:
    return get("speed_unit", "MPH")


def temp_label() -> str:
    return "C" if temp_unit() == "C" else "F"


def speed_label() -> str:
    return "KM/H" if speed_unit() == "KMH" else "MPH"


def toggle_temp_unit() -> None:
    set("temp_unit", "C" if temp_unit() == "F" else "F")


def toggle_speed_unit() -> None:
    set("speed_unit", "KMH" if speed_unit() == "MPH" else "MPH")


# --------------------------------------------------------------------------- #
# AI-vision camera selection. Persisted here; rubyvision reads this same JSON at
# startup (sources._saved_source_pref) and the HUD live-switches the running
# pipeline via a cmd (visionctl.set_source).
# --------------------------------------------------------------------------- #
def vision_source() -> str:
    v = get("vision_source", "usb")
    return v if v in ("usb", "csi") else "usb"


def vision_source_label() -> str:
    return "CSI" if vision_source() == "csi" else "USB"


def cycle_vision_source() -> str:
    nxt = "csi" if vision_source() == "usb" else "usb"
    set("vision_source", nxt)
    return nxt


# --------------------------------------------------------------------------- #
# Shift light: the 4" satellite blanks to amber (speed + gear only) once rpm
# crosses a selectable threshold. The threshold is set from the 7" MENU
# menu and read by satframe in the separate satellite-renderer process -- the
# cross-process reload in _load() is what carries the change across.
# --------------------------------------------------------------------------- #
SHIFT_MIN, SHIFT_MAX, SHIFT_STEP = 3000, 7500, 250


def shift_enabled() -> bool:
    return bool(get("shift_enabled", True))


def toggle_shift_enabled() -> bool:
    v = not shift_enabled()
    set("shift_enabled", v)
    return v


def shift_rpm() -> int:
    """Active shift threshold (rpm), clamped to [SHIFT_MIN, SHIFT_MAX]."""
    try:
        v = int(round(float(get("shift_rpm", 6500))))
    except Exception:
        v = 6500
    return max(SHIFT_MIN, min(SHIFT_MAX, v))


def adjust_shift_rpm(delta: int) -> int:
    """Raise/lower the threshold by `delta`, snapped to the SHIFT_STEP grid and
    clamped to range. Returns the new value (so a tapped row can echo it)."""
    try:
        v = shift_rpm() + int(delta)
    except Exception:
        v = shift_rpm()
    v = max(SHIFT_MIN, min(SHIFT_MAX, v))
    v = SHIFT_MIN + int(round((v - SHIFT_MIN) / float(SHIFT_STEP))) * SHIFT_STEP
    v = max(SHIFT_MIN, min(SHIFT_MAX, v))
    set("shift_rpm", v)
    return v


# --------------------------------------------------------------------------- #
# 4" satellite orientation. Read by satframe in the satellite-renderer process;
# the cross-process reload in _load() carries a 7"-set toggle to the 4".
# MIRROR (horizontal flip) makes the panel read correctly when reflected off
# the windshield (the glass mirrors it back).
# --------------------------------------------------------------------------- #
def sat_mirror() -> bool:
    return bool(get("sat_mirror", False))


def toggle_sat_mirror() -> bool:
    v = not sat_mirror()
    set("sat_mirror", v)
    return v


def sat_rotate() -> bool:
    return bool(get("sat_rotate", False))


def toggle_sat_rotate() -> bool:
    v = not sat_rotate()
    set("sat_rotate", v)
    return v


# AI vision (RealSense + Hailo). Default on; the Pi-4 windshield unit sets this
# False (no PCIe/Hailo) so make_pages() drops the AI page and the menu shows
# RECORDING instead of CAMERA & AI -- one codebase, no per-deploy source edits.
def ai_vision() -> bool:
    return bool(get("ai_vision", True))


def c_to_disp(c: float) -> float:
    """Celsius -> value in the active temperature unit."""
    return c if temp_unit() == "C" else c * 9.0 / 5.0 + 32.0


def mph_to_disp(mph: float) -> float:
    """mph -> value in the active speed unit."""
    return mph * 1.609344 if speed_unit() == "KMH" else mph
