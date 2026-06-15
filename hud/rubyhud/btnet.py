"""bluetoothctl control for the HUD Bluetooth page.

Mirrors wifinet: every slow op (scan, pair, connect, disconnect, forget) runs on
a short-lived background thread that writes lock-guarded caches; the page only
READS those caches (devices()/status()/scanning()/action_state()). poke()
refreshes a stale cache off-thread; rescan() forces a bluetoothctl scan.

Privilege: the HUD service (user michael) is in the `bluetooth` group and the
controller is in dual mode (Classic enabled). v1 pairing is just-works/SSP via a
NoInputNoOutput agent; PIN/passkey-entry UX is a follow-on.

DEMO: on a host without bluetoothctl (the Mac build box) or RUBYHUD_BT_DEMO=1,
canned devices are served so the page renders for layout review.

Everything is failure-guarded and never raises into the render thread.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

_DEVICES_TTL = 4.0          # how long a device snapshot is "fresh"
_PAIR_TIMEOUT = 35.0

_lock = threading.Lock()
_state = {"devices": [], "devices_ts": 0.0, "scanning": False, "powered": True}
_refreshing = False
# action feedback for pair/connect (mirror wifinet._conn): working|ok|failed|idle
_act = {"state": "idle", "mac": None, "name": None, "error": None, "ts": 0.0}

_bt_ok_cache: dict = {}


# --------------------------------------------------------------------------- #
# bluetoothctl availability / demo
# --------------------------------------------------------------------------- #
def demo() -> bool:
    if os.environ.get("RUBYHUD_BT_DEMO") == "1":
        return True
    ok = _bt_ok_cache.get("ok")
    if ok is None:
        try:
            subprocess.run(["bluetoothctl", "--version"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2.0, check=False)
            ok = True
        except Exception:
            ok = False
        _bt_ok_cache["ok"] = ok
    return not ok


# --------------------------------------------------------------------------- #
# bluetoothctl helpers
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=6.0):
    """stdout (str) or '' -- stderr/returncode discarded. Never raises."""
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout,
                             check=False)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _session(cmds, timeout=_PAIR_TIMEOUT):
    """Feed newline-separated commands to interactive bluetoothctl; return its
    combined output. Pairing needs an agent + several steps in one session."""
    try:
        p = subprocess.run(["bluetoothctl"], input=(cmds + "\nquit\n").encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _parse_devices(text):
    """{mac: name} from `bluetoothctl devices*` output ('Device MAC Name')."""
    out = {}
    for line in text.splitlines():
        i = line.find("Device ")
        if i < 0:
            continue
        rest = line[i + 7:].split(" ", 1)
        mac = rest[0].strip()
        if mac.count(":") != 5:
            continue
        out[mac] = rest[1].strip() if len(rest) > 1 and rest[1].strip() else mac
    return out


def _macs(text):
    return set(_parse_devices(text).keys())


def _read_devices():
    alld = _parse_devices(_run(["bluetoothctl", "devices"]))
    paired = _macs(_run(["bluetoothctl", "devices", "Paired"]))
    connected = _macs(_run(["bluetoothctl", "devices", "Connected"]))
    trusted = _macs(_run(["bluetoothctl", "devices", "Trusted"]))
    for m in paired:                       # paired-but-not-in-`devices` (rare)
        alld.setdefault(m, m)
    rows = [{"mac": mac, "name": name, "paired": mac in paired,
             "connected": mac in connected, "trusted": mac in trusted}
            for mac, name in alld.items()]
    rows.sort(key=lambda d: (not d["connected"], not d["paired"],
                             d["name"].lower()))
    return rows


def _read_powered():
    for line in _run(["bluetoothctl", "show"]).splitlines():
        if "Powered:" in line:
            return "yes" in line.split("Powered:", 1)[1].lower()
    return True


def _refresh(do_scan):
    global _refreshing
    try:
        if do_scan:
            # quick read first so paired/known devices show immediately, THEN
            # the (blocking ~12s) timed scan, THEN a final read that adds the
            # newly-discovered devices.
            try:
                pre = _read_devices()
                with _lock:
                    _state["devices"] = pre
                    _state["devices_ts"] = time.monotonic()
            except Exception:
                pass
            _run(["bluetoothctl", "--timeout", "12", "scan", "on"], 16.0)
        devices = _read_devices()
        powered = _read_powered()
        now = time.monotonic()
        with _lock:
            _state["devices"] = devices
            _state["devices_ts"] = now
            _state["powered"] = powered
    except Exception:
        pass
    finally:
        with _lock:
            _state["scanning"] = False
        _refreshing = False


def _spawn_refresh(do_scan):
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
        if do_scan:
            _state["scanning"] = True
    threading.Thread(target=_refresh, args=(do_scan,), name="rubyhud-bt",
                     daemon=True).start()


# --------------------------------------------------------------------------- #
# public API (page reads caches; never blocks)
# --------------------------------------------------------------------------- #
def poke():
    if demo():
        return
    with _lock:
        stale = time.monotonic() - _state["devices_ts"] >= _DEVICES_TTL
    if stale:
        _spawn_refresh(False)


def rescan():
    if demo():
        return
    _spawn_refresh(True)


def scanning() -> bool:
    with _lock:
        return bool(_state["scanning"])


def set_pairable(on: bool) -> None:
    """Make the controller discoverable+pairable (so phones can also pair TO the
    Pi) while the page is open. Off-thread; best-effort."""
    if demo():
        return
    v = "on" if on else "off"
    threading.Thread(
        target=lambda: _session("discoverable %s\npairable %s" % (v, v), 8.0),
        name="rubyhud-bt-pairable", daemon=True).start()


def devices() -> list:
    if demo():
        return [
            {"mac": "AA:11:22:33:44:55", "name": "Michael’s AirPods",
             "paired": True, "connected": True, "trusted": True},
            {"mac": "BB:22:33:44:55:66", "name": "MAZDA",
             "paired": True, "connected": False, "trusted": True},
            {"mac": "CC:33:44:55:66:77", "name": "Magic Keyboard",
             "paired": True, "connected": False, "trusted": True},
            {"mac": "DD:44:55:66:77:88", "name": "JBL Flip 6",
             "paired": False, "connected": False, "trusted": False},
            {"mac": "EE:55:66:77:88:99", "name": "Galaxy Buds",
             "paired": False, "connected": False, "trusted": False},
        ]
    with _lock:
        return [dict(d) for d in _state["devices"]]


def status() -> dict:
    if demo():
        return {"powered": True}
    with _lock:
        return {"powered": bool(_state["powered"])}


def action_state() -> dict:
    with _lock:
        return dict(_act)


def _set_act(state, mac=None, name=None, error=None):
    with _lock:
        _act["state"] = state
        if mac is not None:
            _act["mac"] = mac
        if name is not None:
            _act["name"] = name
        _act["error"] = error
        _act["ts"] = time.monotonic()


def _begin(mac, name) -> bool:
    with _lock:
        if _act["state"] == "working":
            return False
        _act.update({"state": "working", "mac": mac, "name": name,
                     "error": None, "ts": time.monotonic()})
    return True


def _is_connected(mac):
    return mac in _macs(_run(["bluetoothctl", "devices", "Connected"]))


def _is_paired(mac):
    return mac in _macs(_run(["bluetoothctl", "devices", "Paired"]))


def _pair_worker(mac, name):
    if demo():
        _set_act("ok", mac, name)
        return
    # NoInputNoOutput agent => just-works/SSP auto-accept; then pair/trust/connect
    _session("agent NoInputNoOutput\ndefault-agent\npair %s\ntrust %s\nconnect %s"
             % (mac, mac, mac))
    time.sleep(0.5)
    if _is_connected(mac) or _is_paired(mac):
        _set_act("ok", mac, name)
    else:
        _set_act("failed", mac, name,
                 "pairing failed (device may need a PIN — not yet supported)")
    _spawn_refresh(False)


def pair(mac, name=None) -> bool:
    if not mac or not _begin(mac, name):
        return False
    threading.Thread(target=_pair_worker, args=(mac, name),
                     name="rubyhud-bt-pair", daemon=True).start()
    return True


def _connect_worker(mac, name):
    if demo():
        _set_act("ok", mac, name)
        return
    _run(["bluetoothctl", "connect", mac], 25.0)
    time.sleep(0.3)
    ok = _is_connected(mac)
    _set_act("ok" if ok else "failed", mac, name,
             None if ok else "connect failed")
    _spawn_refresh(False)


def connect(mac, name=None) -> bool:
    if not mac or not _begin(mac, name):
        return False
    threading.Thread(target=_connect_worker, args=(mac, name),
                     name="rubyhud-bt-conn", daemon=True).start()
    return True


def _simple_worker(cmd):
    if demo():
        return
    _run(cmd, 20.0)
    _spawn_refresh(False)


def disconnect(mac):
    if not mac:
        return
    with _lock:
        _act["state"] = "idle"
    threading.Thread(target=_simple_worker,
                     args=(["bluetoothctl", "disconnect", mac],),
                     name="rubyhud-bt-disc", daemon=True).start()


def forget(mac):
    if not mac:
        return
    threading.Thread(target=_simple_worker,
                     args=(["bluetoothctl", "remove", mac],),
                     name="rubyhud-bt-forget", daemon=True).start()


# Warm the availability probe off the render thread (see wifinet rationale).
def _warm_probe():
    try:
        demo()
    except Exception:
        pass


threading.Thread(target=_warm_probe, name="rubyhud-bt-probe",
                 daemon=True).start()
