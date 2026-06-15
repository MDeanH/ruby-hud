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

_DEFAULTS = {
    "temp_unit": "F",      # "F" | "C"
    "speed_unit": "MPH",   # "MPH" | "KMH"
    "vision_source": "usb",  # "usb" | "csi" — AI-vision camera (rubyvision)
}

_PATH = os.environ.get(
    "RUBYHUD_CONFIG",
    os.path.join(os.path.expanduser("~"), "hud-state", "rubyhud-config.json"))

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
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
    _cache = data
    return data


def get(key: str, default=None):
    with _lock:
        d = _load()
        if default is None:
            default = _DEFAULTS.get(key)
        return d.get(key, default)


def set(key: str, value) -> None:
    """Update one key and persist atomically. Never raises."""
    with _lock:
        d = _load()
        d[key] = value
        try:
            os.makedirs(os.path.dirname(_PATH), exist_ok=True)
            tmp = _PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh)
            os.replace(tmp, _PATH)
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


def c_to_disp(c: float) -> float:
    """Celsius -> value in the active temperature unit."""
    return c if temp_unit() == "C" else c * 9.0 / 5.0 + 32.0


def mph_to_disp(mph: float) -> float:
    """mph -> value in the active speed unit."""
    return mph * 1.609344 if speed_unit() == "KMH" else mph
