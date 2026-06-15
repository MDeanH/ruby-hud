"""Control the rubyvision systemd service from the HUD.

Shared by the AI VISION page (on-page power button) and CONFIGURE > AI VISION.
Status (`systemctl is-active`) is TTL-cached so per-frame value_fns / render
calls never shell out every frame; start/stop is fire-and-forget via the scoped
sudoers rule (deploy/sudoers.d/rubyhud-vision) so the render thread never blocks
on the unit operation. Never raises.
"""

from __future__ import annotations

import subprocess
import time

from .signals import _run

_TTL = 2.5
_status = {"t": 0.0, "active": False}


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


def status_label() -> str:
    return "ON" if is_active() else "off"
