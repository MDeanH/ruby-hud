"""Control the rubyvision systemd service from the HUD.

Shared by the AI VISION page (on-page power button) and MENU > AI VISION.
Status (`systemctl is-active`) is TTL-cached so per-frame value_fns / render
calls never shell out every frame; start/stop is fire-and-forget via the scoped
sudoers rule (deploy/sudoers.d/rubyhud-vision) so the render thread never blocks
on the unit operation. Never raises.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from .signals import _run

_TTL = 2.5
_status = {"t": 0.0, "active": False}

_VISION_DIR = os.environ.get("RUBYVISION_SHM", "/dev/shm/rubyvision")
_cmd_seq = {"n": 0}


def is_active() -> bool:
    now = time.monotonic()
    if now - _status["t"] >= _TTL:
        out = _run(["systemctl", "is-active", "rubyvision"], timeout=2.0)
        _status["active"] = (out or "").strip() == "active"
        _status["t"] = now
    return _status["active"]


def set_active(on: bool) -> None:
    action = "start" if on else "stop"
    try:
        subprocess.Popen(
            ["sudo", "-n", "/usr/bin/systemctl", action, "rubyvision"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _status["t"] = 0.0   # force a fresh status read on the next frame


def toggle() -> None:
    set_active(not is_active())


def restart() -> None:
    """Restart rubyvision so it re-detects + re-opens the camera (for swapping
    cameras on the bench / recovering a wedged camera). Fire-and-forget via the
    scoped sudoers rule; the render thread never blocks. Never raises."""
    try:
        subprocess.Popen(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "rubyvision"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _status["t"] = 0.0   # force a fresh status read on the next frame


def status_label() -> str:
    return "ON" if is_active() else "off"


def set_source(name: str) -> None:
    """Live-switch the running rubyvision pipeline's camera by writing cmd.json
    (mtime-gated, so the pipeline consumes it once). The persistent choice lives
    in config.vision_source(); this only applies it to the running process and
    is a no-op if vision is off / the shm dir is missing. Never raises."""
    try:
        _cmd_seq["n"] += 1
        os.makedirs(_VISION_DIR, exist_ok=True)
        path = os.path.join(_VISION_DIR, "cmd.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"seq": _cmd_seq["n"], "cmd": "set_source",
                       "source": name}, fh)
        os.replace(tmp, path)
    except Exception:
        pass
