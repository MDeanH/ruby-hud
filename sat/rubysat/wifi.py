"""Read the Ruby Pi's active Wi-Fi credentials (nmcli, guarded).

Used when the Qualia satellite asks for a wifi_sync over the USB link so the
4" display can join the same network as the Pi without retyping secrets in
secrets.h. Every call is failure-guarded and NEVER raises.

Only the active/default Wi-Fi connection is returned. Password read requires
nmcli and appropriate permissions (NetworkManager often allows the michael
user to read saved secrets on a headless Pi).
"""

from __future__ import annotations

import subprocess
import time

_cache: dict = {}


def _cached(key, ttl, fn):
    now = time.monotonic()
    ent = _cache.get(key)
    if ent is not None and now - ent[0] < ttl:
        return ent[1]
    try:
        val = fn()
    except Exception:
        val = None
    _cache[key] = (now, val)
    return val


def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    try:
        out = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def active_ssid() -> str | None:
    """SSID of the Pi's active Wi-Fi connection, or None."""

    def read():
        out = _run(["iwgetid", "-r"], timeout=2.0)
        if out:
            ssid = out.strip()
            if ssid:
                return ssid
        out = _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show",
                    "wlan0"], timeout=2.0)
        if out:
            name = out.strip()
            if name and name != "--":
                return name
        return None

    return _cached("ssid", 10.0, read)


def active_wifi() -> tuple[str | None, str | None]:
    """(ssid, password) for the Pi's active Wi-Fi, or (None, None)."""

    def read():
        ssid = active_ssid()
        if not ssid:
            return None, None
        psk = _run([
            "nmcli", "-s", "-g", "802-11-wireless-security.psk",
            "con", "show", ssid,
        ], timeout=2.0)
        pwd = None
        if psk:
            psk = psk.strip()
            if psk and psk != "--":
                pwd = psk
        return ssid, pwd

    result = _cached("wifi", 30.0, read)
    if result is None:
        return None, None
    return result
