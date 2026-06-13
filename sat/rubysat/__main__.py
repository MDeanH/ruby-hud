"""rubysat entrypoint: python -m rubysat [options].

Serves the STATE channel to Qualia satellite client(s) over TCP and/or USB.

Options:
  --channel can0     socketcan channel for the DataLayer. Default can0.
  --port 7878        TCP listen port (server binds 0.0.0.0). Default 7878.
  --hz 15            target STATE publish rate. Default 15.
  --novehicle        skip the DataLayer entirely and serve demo snapshots
                     (bench / build-host use, no can0 needed).
  --no-serial        disable the USB-CDC serial link (TCP only).

Loop each tick:
  1. snapshot vehicle state (DataLayer.snapshot() or demo_snapshot()),
  2. read cached vision status (never raises),
  3. read SoC temperature,
  4. build_state() -> JSON line, broadcast() to TCP + USB clients,
  5. drain inbound CMD lines from both transports: allowlisted ruby_* control
     verbs map to ruby-updated queue commands; wifi_sync (USB) returns Pi Wi-Fi
     credentials; page_prev/page_next forward to rubyhud.

After a verb is handled, a transient "ack" key ("<verb>:sent|failed") rides
along in STATE lines for ~ACK_TTL_S seconds, then is dropped again. Old
clients that don't know the key simply ignore it (schema is otherwise
unchanged).

A heartbeat guarantees >= 2 Hz output even if the publish rate is set lower.
SIGTERM / SIGINT shut the loop and server down cleanly.

If rubyhud is not importable (e.g. on a build host) the service automatically
falls back to demo data so it still serves -- it never hard-fails on import.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from .publisher import TcpStateServer
from .seriallink import SerialStateLink
from .state import build_state
from . import wifi as pi_wifi

_LOG = "/tmp/rubysat.log"
# Vision status drop written by rubyvision.
VISION_STATUS_PATH = "/dev/shm/rubyvision/status.json"
# Heartbeat floor: publish at least this often even if --hz is lower or a
# snapshot read stalls, so the Qualia's link-alive timer never trips on a
# healthy-but-quiet bus.
HEARTBEAT_HZ = 2.0
SOC_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"

# ---- Qualia control verbs -------------------------------------------------- #
# Allowlist: Qualia menu verb -> (ruby-updated queue cmd, ref). The queue cmds
# are the ones allowlisted by the root updater's path unit. Anything not in
# this map (page_prev / page_next / tap, junk) keeps v1 log-only behavior.
VERB_MAP = {
    "ruby_check": ("check", None),
    "ruby_update": ("apply", None),
    "ruby_rollback": ("rollback", None),
    "ruby_restart_hud": ("restart-hud", None),
    "ruby_switch_dash": ("switch-dash", None),
}
# How long a verb ack ("<verb>:sent|failed") rides along in STATE lines.
ACK_TTL_S = 3.0
# Fallback queue location when rubyhud.updates is not importable. Honors the
# same env override as rubyhud.updates so bench tests can point it at a tmpdir.
_UPDATE_DIR_DEFAULT = "/run/ruby-update"
# Bound per-command log line length (inbound CMD json is client-controlled).
_LOG_CMD_MAX = 200


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _read_soc_temp():
    """SoC temperature in C, or None. Never raises."""
    try:
        with open(SOC_TEMP_PATH) as fh:
            return int(fh.read().strip()) / 1000.0
    except Exception:
        return None


class _VisionCache:
    """Caches the vision status.json, re-reading at most a few times a second
    and only when the file mtime changes. Never raises; returns last-good (or
    None) on any error so the publish loop is fully decoupled from vision."""

    def __init__(self, path: str = VISION_STATUS_PATH, min_interval: float = 0.1):
        self.path = path
        self.min_interval = min_interval
        self._last_check = 0.0
        self._last_mtime = -1.0
        self._cached = None

    def get(self):
        now = time.monotonic()
        if now - self._last_check < self.min_interval:
            return self._cached
        self._last_check = now
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._cached = None
            return None
        except Exception:
            return self._cached  # keep last-good on transient stat error
        if st.st_mtime == self._last_mtime:
            return self._cached
        try:
            with open(self.path, "r") as fh:
                self._cached = json.load(fh)
            self._last_mtime = st.st_mtime
        except Exception:
            # Mid-write or malformed: keep last-good; staleness logic in
            # build_state() will eventually fall back to "off".
            pass
        return self._cached


def _queue_update_request(cmd: str, ref=None) -> bool:
    """Queue an updater command for ruby-updated. Returns True when queued.
    NEVER raises.

    Prefers rubyhud.updates.request() (the canonical writer; import guarded at
    use-site). When rubyhud is not importable (build host, broken install) it
    falls back to a self-contained atomic write of {"cmd":...[,"ref":...],
    "ts":...} into <update-dir>/queue: mkstemp in-dir with a DOT-PREFIXED temp
    name (the path unit's *.req glob must never fire on a half-written file)
    then os.replace() into the final *.req name."""
    try:
        try:
            from rubyhud import updates
        except Exception:
            updates = None
        if updates is not None:
            return bool(updates.request(cmd, ref))

        # ---- self-contained fallback (no rubyhud on this host) ---- #
        import tempfile
        qdir = os.path.join(
            os.environ.get("RUBYHUD_UPDATE_DIR", _UPDATE_DIR_DEFAULT), "queue")
        payload = {"cmd": str(cmd)}
        if ref:
            payload["ref"] = str(ref)
        payload["ts"] = round(time.time(), 3)
        fd, tmp = tempfile.mkstemp(prefix=".req-", dir=qdir)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(payload) + "\n")
            dst = os.path.join(qdir, "%d-%d.req"
                               % (time.time_ns() // 1000000, os.getpid()))
            os.replace(tmp, dst)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return False
    except Exception:
        return False


_REMOTE_CMD_PATH = "/dev/shm/rubyhud-remote.json"
_remote_seq = [0]


def _forward_hud_page(cmd: str) -> None:
    """Bridge satellite page buttons to the local rubyhud page rotation.

    Atomic write of {"seq", "cmd", "ts"} to /dev/shm/rubyhud-remote.json;
    rubyhud's main loop consumes it (mtime + seq gated). Never raises."""
    try:
        import json as _json
        import tempfile as _tf
        _remote_seq[0] += 1
        payload = _json.dumps({"seq": _remote_seq[0], "cmd": cmd,
                               "ts": round(time.time(), 3)})
        d = os.path.dirname(_REMOTE_CMD_PATH)
        fd, tmp = _tf.mkstemp(prefix=".rhr-", dir=d)
        try:
            os.write(fd, payload.encode("ascii"))
        finally:
            os.close(fd)
        os.replace(tmp, _REMOTE_CMD_PATH)
    except Exception:
        pass


_CTL_PATH = "/dev/shm/rubysat-ctl.json"
_ctl_state = {"mtime": 0, "doc": None, "until": 0.0}


def _poll_ctl() -> None:
    """Pick up satellite-control commands written by rubyhud (the 7" Settings
    SATELLITE submenu) and ride them on STATE lines for ~3s (seq-deduped on
    the Qualia). mtime-gated; never raises."""
    try:
        st = os.stat(_CTL_PATH)
    except OSError:
        return
    if st.st_mtime_ns == _ctl_state["mtime"]:
        return
    _ctl_state["mtime"] = st.st_mtime_ns
    try:
        with open(_CTL_PATH) as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and doc.get("cmd"):
            _ctl_state["doc"] = {"seq": int(doc.get("seq", 0)),
                                 "cmd": str(doc.get("cmd"))[:24]}
            _ctl_state["until"] = time.monotonic() + 3.0
    except Exception:
        pass


def _handle_command(cmd, ack: dict, serial=None) -> None:
    """Route one inbound Qualia CMD dict. NEVER raises; logging is bounded.

    Allowlisted ruby_* verbs queue an updater request and arm a transient ack
    ("<verb>:sent|failed") that the publish loop attaches to STATE lines for
    ACK_TTL_S seconds. wifi_sync replies on the USB link only. Everything else
    (page_prev / page_next / tap, unknown junk) keeps the v1 log-only behavior."""
    try:
        try:
            line = json.dumps(cmd, separators=(",", ":"))
        except Exception:
            line = repr(cmd)
        _log("CMD %s" % line[:_LOG_CMD_MAX])

        verb = cmd.get("cmd") if isinstance(cmd, dict) else None
        if verb == "wifi_sync":
            ssid, pwd = pi_wifi.active_wifi()
            ok = False
            if ssid and serial is not None and serial.connected:
                payload = {"type": "wifi", "ssid": ssid, "pass": pwd or ""}
                ok = serial.send_line(json.dumps(payload, separators=(",", ":")))
            ack["text"] = "wifi_sync:%s" % ("sent" if ok else "failed")
            ack["until"] = time.monotonic() + ACK_TTL_S
            _log("wifi_sync -> %s" % ("sent" if ok else "failed"))
            return
        if verb in ("page_next", "page_prev"):
            _forward_hud_page(verb)
            return
        mapped = VERB_MAP.get(verb) if isinstance(verb, str) else None
        if mapped is None:
            return  # log-only: taps and anything not allowlisted
        ucmd, ref = mapped
        ok = _queue_update_request(ucmd, ref)
        result = "sent" if ok else "failed"
        ack["text"] = "%s:%s" % (verb, result)
        ack["until"] = time.monotonic() + ACK_TTL_S
        _log("VERB %s -> updater %s: %s" % (verb, ucmd, result))
    except Exception:
        # A misbehaving client must never take the publish loop down.
        pass


def _make_snapshot_source(channel: str, novehicle: bool):
    """Return (snapshot_fn, stop_fn). Falls back to demo data if rubyhud is not
    importable or --novehicle is set."""
    if not novehicle:
        try:
            from rubyhud.signals import DataLayer
        except Exception as exc:
            _log("rubyhud import failed (%s); falling back to demo data" % exc)
            novehicle = True

    if novehicle:
        try:
            from rubyhud.signals import DataLayer as _DL
            demo = _DL.demo_snapshot
        except Exception:
            demo = _local_demo_snapshot
        return demo, (lambda: None)

    data = DataLayer(channel)
    data.start()
    return data.snapshot, data.stop


def _local_demo_snapshot():
    """Self-contained demo snapshot for when rubyhud isn't importable at all.

    Mirrors the fields build_state() reads off a Snapshot. Returned as a tiny
    object so attribute access matches the real dataclass.

    The numeric fields gently animate (a slow sine sweep keyed off wall time) so
    bench output ANIMATES the same way on any host -- previously this returned
    constant values, so a bench run without rubyhud showed frozen gauges and
    could mask gauge-animation issues. Deterministic, no per-call state."""
    import math

    phase = time.time() / 4.0  # ~25 s period
    osc = math.sin(phase)              # -1..1
    osc01 = 0.5 + 0.5 * osc            # 0..1

    class _Demo:
        speed_mph = int(62 + 18 * osc)            # 44..80
        rpm = int(4200 + 1800 * osc)              # 2400..6000
        gear = "4"
        coolant_c = int(92 + 6 * osc)             # 86..98
        volts = round(14.1 + 0.3 * osc, 1)        # 13.8..14.4
        throttle_pct = int(38 + 30 * osc01)       # 38..68
        fuel_pct = 64
        source = "SIM"
        can_fps = 480
        can_bus_state = "UP"
    return _Demo()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rubysat", description="Ruby STATE publisher for the Qualia satellite")
    ap.add_argument("--channel", default="can0", help="socketcan channel")
    ap.add_argument("--port", type=int, default=7878, help="TCP listen port")
    ap.add_argument("--hz", type=float, default=15.0, help="STATE publish rate")
    ap.add_argument("--novehicle", action="store_true",
                    help="skip DataLayer; serve demo snapshots (bench)")
    ap.add_argument("--no-serial", action="store_true",
                    help="disable USB-CDC serial link (TCP only)")
    args = ap.parse_args(argv)

    hz = args.hz if args.hz > 0 else 15.0
    period = 1.0 / hz
    heartbeat = 1.0 / HEARTBEAT_HZ

    server = TcpStateServer(host="0.0.0.0", port=args.port)
    try:
        server.start()
    except Exception as exc:
        _log("server start failed on port %d: %s" % (args.port, exc))
        print("rubysat: failed to bind port %d: %s" % (args.port, exc),
              file=sys.stderr)
        return 1
    _log("rubysat listening on 0.0.0.0:%d at %.1f Hz" % (args.port, hz))

    serial = None if args.no_serial else SerialStateLink()
    if serial is not None:
        _log("rubysat USB serial link enabled")

    snapshot_fn, stop_fn = _make_snapshot_source(args.channel, args.novehicle)
    vision = _VisionCache()

    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    seq = 0
    last_emit = 0.0
    # Transient verb ack: text rides in STATE lines until the monotonic
    # deadline passes, then the key is dropped (schema stays compatible).
    ack = {"text": None, "until": 0.0}
    try:
        while not stop["flag"]:
            now = time.monotonic()
            if serial is not None:
                serial.pump()
            # Emit on the publish period, or sooner only via the period gate;
            # the heartbeat floor matters when period > heartbeat (low --hz).
            due = (now - last_emit) >= min(period, heartbeat)
            if not due:
                # Sleep the smaller of the remaining time to next emit; keep it
                # short so SIGTERM is responsive.
                time.sleep(min(0.02, period))
                continue

            try:
                snap = snapshot_fn()
            except Exception as exc:
                _log("snapshot failed: %s" % exc)
                snap = _local_demo_snapshot()

            vstatus = vision.get()
            soc = _read_soc_temp()
            t = time.time()
            try:
                state = build_state(snap, vstatus, soc, seq, t)
                pi_ssid = pi_wifi.active_ssid()
                if pi_ssid:
                    state["pi_ssid"] = pi_ssid
                if ack["text"] is not None:
                    if now < ack["until"]:
                        state["ack"] = ack["text"]
                    else:
                        ack["text"] = None  # TTL expired: drop the key again
                _poll_ctl()
                if _ctl_state["doc"] is not None:
                    if time.monotonic() < _ctl_state["until"]:
                        state["ctl"] = _ctl_state["doc"]
                    else:
                        _ctl_state["doc"] = None
                line = json.dumps(state, separators=(",", ":"))
            except Exception as exc:
                _log("build_state failed: %s" % exc)
                last_emit = now
                continue

            server.broadcast(line)
            if serial is not None:
                serial.broadcast(line)
            seq += 1
            last_emit = now

            for cmd in server.commands():
                _handle_command(cmd, ack, serial)
            if serial is not None:
                for cmd in serial.commands():
                    _handle_command(cmd, ack, serial)
    finally:
        _log("rubysat shutting down")
        try:
            server.stop()
        except Exception:
            pass
        if serial is not None:
            try:
                serial.stop()
            except Exception:
                pass
        try:
            stop_fn()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
