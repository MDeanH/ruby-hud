"""NetworkManager (nmcli) control for the HUD WiFi page.

The render loop must never block, so every slow operation (scan/rescan,
connect, disconnect, forget) runs on a short-lived background thread that
updates shared, lock-guarded caches; the page only ever READS those caches
(status()/networks()/saved()/connect_state()). poke() opportunistically
refreshes a stale cache off-thread; rescan() forces an nmcli rescan.

Privilege: connect/modify/scan need NetworkManager polkit actions that the
HUD service (user `michael`) gets via the netdev polkit rule shipped in
deploy/polkit/ (see that file). Credentials are owned by NetworkManager
(/etc/NetworkManager/system-connections/*, root 0600) -- this module never
stores or logs a password.

DEMO: on a host without nmcli (the Mac build box) or with RUBYHUD_WIFI_DEMO=1,
canned data is served so the page renders for oneshot/layout review.

Everything is failure-guarded and never raises into the render thread.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

_STATUS_TTL = 3.0          # how long a status/networks snapshot is "fresh"
_SAVED_TTL = 8.0
_CONNECT_TIMEOUT = 45.0    # nmcli connect/up wall timeout

_lock = threading.Lock()
_state = {
    "status": {"connected": False, "ssid": None, "ip": None,
               "signal": None, "security": None},
    "networks": [],        # [{ssid, signal, security, in_use, saved}]
    "saved": [],           # [{name, ssid}]
    "status_ts": 0.0,
    "saved_ts": 0.0,
    "scanning": False,
}
_refreshing = False
_conn = {"state": "idle", "ssid": None, "error": None, "ts": 0.0}

# iPhone hotspot profile name (car use); matched loosely if renamed.
_HOTSPOT_HINTS = ("iphone-hotspot", "iphone", "hotspot")


# --------------------------------------------------------------------------- #
# nmcli availability / demo
# --------------------------------------------------------------------------- #
_nmcli_ok_cache: dict = {}


def demo() -> bool:
    if os.environ.get("RUBYHUD_WIFI_DEMO") == "1":
        return True
    ok = _nmcli_ok_cache.get("ok")
    if ok is None:
        try:
            subprocess.run(["nmcli", "--version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2.0, check=False)
            ok = True
        except Exception:
            ok = False
        _nmcli_ok_cache["ok"] = ok
    return not ok


# --------------------------------------------------------------------------- #
# nmcli helpers
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=4.0):
    """stdout (str) or '' -- stderr/returncode discarded. Never raises."""
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout,
                             check=False)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _run_rc(cmd, timeout=_CONNECT_TIMEOUT):
    """(returncode, stderr_tail) for a mutating op. Never raises; never logs
    the command (it may carry a password). rc=None on exec failure/timeout."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        return p.returncode, err.splitlines()[-1] if err else ""
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except Exception:
        return None, "error"


def _split_terse(line: str) -> list:
    """Split an `nmcli -t` line, honoring its backslash escaping of ':'/'\\'."""
    out, cur, i, n = [], [], 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def is_secured(security) -> bool:
    """True when a network's SECURITY field denotes an encrypted network."""
    s = (security or "").strip()
    return bool(s) and s not in ("--", "open", "OPEN", "none", "None")


# --------------------------------------------------------------------------- #
# readers (run on a background thread, write the caches)
# --------------------------------------------------------------------------- #
def _read_saved():
    """[{name, ssid}] for every 802-11-wireless profile."""
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], 4.0)
    saved = []
    for line in out.splitlines():
        f = _split_terse(line)
        if len(f) >= 2 and f[1] == "802-11-wireless" and f[0]:
            name = f[0]
            sout = _run(["nmcli", "-t", "-f", "802-11-wireless.ssid",
                         "connection", "show", name], 4.0)
            ssid = None
            for sl in sout.splitlines():
                sf = _split_terse(sl)
                if len(sf) >= 2 and sf[0] == "802-11-wireless.ssid":
                    ssid = sf[1] or None
                    break
            saved.append({"name": name, "ssid": ssid})
    return saved


def _read_status():
    out = _run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                "dev", "wifi"], 4.0)
    st = {"connected": False, "ssid": None, "ip": None,
          "signal": None, "security": None}
    for line in out.splitlines():
        f = _split_terse(line)
        if len(f) >= 4 and f[0] == "*":
            st["connected"] = True
            st["ssid"] = f[1] or None
            try:
                st["signal"] = int(f[2])
            except Exception:
                st["signal"] = None
            st["security"] = f[3] or None
            break
    parts = _run(["hostname", "-I"], 2.0).split()
    st["ip"] = parts[0] if parts else None
    return st


def _read_networks(saved):
    saved_ssids = {s["ssid"] for s in saved if s.get("ssid")}
    out = _run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                "dev", "wifi", "list"], 8.0)
    seen: dict = {}
    for line in out.splitlines():
        f = _split_terse(line)
        if len(f) < 4 or not f[1]:        # skip hidden / blank-SSID rows
            continue
        ssid = f[1]
        try:
            signal = int(f[2])
        except Exception:
            signal = 0
        entry = {"ssid": ssid, "signal": signal, "security": f[3],
                 "in_use": f[0] == "*", "saved": ssid in saved_ssids}
        prev = seen.get(ssid)
        if prev is None or signal > prev["signal"]:
            entry["in_use"] = entry["in_use"] or (prev or {}).get("in_use", False)
            seen[ssid] = entry
    return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)


def _refresh(do_rescan: bool):
    global _refreshing
    try:
        if do_rescan:
            _run(["nmcli", "dev", "wifi", "rescan"], 8.0)  # may rate-limit; ok
            time.sleep(1.2)                                # let results settle
        saved = _read_saved()
        status = _read_status()
        networks = _read_networks(saved)
        now = time.monotonic()
        with _lock:
            _state["saved"] = saved
            _state["saved_ts"] = now
            _state["status"] = status
            _state["networks"] = networks
            _state["status_ts"] = now
    except Exception:
        pass
    finally:
        with _lock:
            _state["scanning"] = False
        _refreshing = False


def _spawn_refresh(do_rescan: bool):
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
        if do_rescan:
            _state["scanning"] = True
    threading.Thread(target=_refresh, args=(do_rescan,),
                     name="rubyhud-wifi", daemon=True).start()


# --------------------------------------------------------------------------- #
# public API (page reads caches; never blocks)
# --------------------------------------------------------------------------- #
def poke():
    """Refresh the cache off-thread if it is stale (no forced rescan)."""
    if demo():
        return
    with _lock:
        stale = time.monotonic() - _state["status_ts"] >= _STATUS_TTL
    if stale:
        _spawn_refresh(False)


def rescan():
    """Force an nmcli rescan off-thread; UI shows 'scanning' until it lands."""
    if demo():
        return
    _spawn_refresh(True)


def scanning() -> bool:
    with _lock:
        return bool(_state["scanning"])


def status() -> dict:
    if demo():
        return {"connected": True, "ssid": "Michael’s iPhone",
                "ip": "172.20.10.4", "signal": 59, "security": "WPA2"}
    with _lock:
        return dict(_state["status"])


def networks() -> list:
    if demo():
        return [
            {"ssid": "Michael’s iPhone", "signal": 59, "security": "WPA2",
             "in_use": True, "saved": True},
            {"ssid": "CarplayBox_A672", "signal": 100, "security": "WPA2",
             "in_use": False, "saved": False},
            {"ssid": "WWWFM330", "signal": 47, "security": "WPA2",
             "in_use": False, "saved": False},
            {"ssid": "SPOTA", "signal": 47, "security": "WPA2",
             "in_use": False, "saved": False},
            {"ssid": "12edaf42", "signal": 45, "security": "WPA2",
             "in_use": False, "saved": False},
            {"ssid": "WWWFM308", "signal": 44, "security": "WPA2",
             "in_use": False, "saved": False},
            {"ssid": "xfinitywifi", "signal": 28, "security": "",
             "in_use": False, "saved": False},
        ]
    with _lock:
        return [dict(n) for n in _state["networks"]]


def saved() -> list:
    if demo():
        return [{"name": "iphone-hotspot", "ssid": "Michael’s iPhone"},
                {"name": "rubywifi", "ssid": "Andanotherone"}]
    with _lock:
        return [dict(s) for s in _state["saved"]]


def saved_name_for(ssid):
    """Profile name whose ssid matches, or None."""
    for s in saved():
        if s.get("ssid") == ssid:
            return s.get("name")
    return None


def hotspot_name():
    """Best saved-profile name for the car iPhone hotspot, or None."""
    rows = saved()
    for hint in _HOTSPOT_HINTS:
        for s in rows:
            if hint in (s.get("name") or "").lower():
                return s.get("name")
    return None


def connect_state() -> dict:
    with _lock:
        return dict(_conn)


def _set_conn(state, ssid=None, error=None):
    with _lock:
        _conn["state"] = state
        if ssid is not None:
            _conn["ssid"] = ssid
        _conn["error"] = error
        _conn["ts"] = time.monotonic()


def _begin(ssid) -> bool:
    """Mark a connect in flight; returns False if one is already running."""
    with _lock:
        if _conn["state"] == "connecting":
            return False
        _conn["state"] = "connecting"
        _conn["ssid"] = ssid
        _conn["error"] = None
        _conn["ts"] = time.monotonic()
    return True


def _connect_worker(cmd, ssid):
    if demo():
        _set_conn("ok", ssid)
        return
    rc, err = _run_rc(cmd)
    if rc == 0:
        _set_conn("ok", ssid)
    else:
        _set_conn("failed", ssid, err or "connection failed")
    _spawn_refresh(False)


def connect(ssid, password=None) -> bool:
    """Join a network (new profile); secured networks need a password.
    Returns True iff a connect attempt was actually started (so the caller
    only shows the 'connecting' screen when something is really in flight)."""
    if not ssid or not _begin(ssid):
        return False
    # `--` ends nmcli option parsing: a beacon SSID beginning with '-' (even a
    # benign "-Guest") is then treated as the SSID, not parsed as a flag.
    cmd = ["nmcli", "dev", "wifi", "connect", "--", ssid]
    if password:
        cmd += ["password", password]
    threading.Thread(target=_connect_worker, args=(cmd, ssid),
                     name="rubyhud-wifi-connect", daemon=True).start()
    return True


def connect_saved(name, ssid=None) -> bool:
    """Activate an existing saved profile by name. Returns True iff started."""
    if not name or not _begin(ssid or name):
        return False
    cmd = ["nmcli", "connection", "up", "id", name]
    threading.Thread(target=_connect_worker, args=(cmd, ssid or name),
                     name="rubyhud-wifi-up", daemon=True).start()
    return True


def hotspot() -> bool:
    """Bring up the saved iPhone-hotspot profile (car mode). Returns True iff a
    saved hotspot profile existed and a connect was actually started."""
    name = hotspot_name()
    return connect_saved(name) if name else False


def _simple_worker(cmd, label, refresh=True):
    if demo():
        return
    _run_rc(cmd, 20.0)
    if refresh:
        _spawn_refresh(False)


def disconnect():
    """Drop the current WiFi association (keeps the saved profile)."""
    threading.Thread(
        target=_simple_worker,
        args=(["nmcli", "device", "disconnect", "wlan0"], "disconnect"),
        name="rubyhud-wifi-down", daemon=True).start()
    with _lock:
        _conn["state"] = "idle"


def forget(name):
    """Delete a saved profile."""
    if not name:
        return
    threading.Thread(
        target=_simple_worker,
        args=(["nmcli", "connection", "delete", "id", name], "forget"),
        name="rubyhud-wifi-forget", daemon=True).start()


# Warm the nmcli-availability probe off the render thread: demo() runs a
# synchronous `nmcli --version` on first call, and the page hits demo() from
# render()/on_show() (the 15fps thread). This module is imported at HUD startup
# (make_pages -> WiFiPage), so the probe finishes well before the user ever
# opens the page; the render-thread demo() then just reads the warm cache.
def _warm_probe():
    try:
        demo()
    except Exception:
        pass


threading.Thread(target=_warm_probe, name="rubyhud-wifi-probe",
                 daemon=True).start()
