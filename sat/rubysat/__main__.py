"""rubysat entrypoint: python -m rubysat [options].

Serves the STATE channel to Qualia satellite client(s) over TCP.

Options:
  --channel can0     socketcan channel for the DataLayer. Default can0.
  --port 7878        TCP listen port (server binds 0.0.0.0). Default 7878.
  --hz 15            target STATE publish rate. Default 15.
  --novehicle        skip the DataLayer entirely and serve demo snapshots
                     (bench / build-host use, no can0 needed).

Loop each tick:
  1. snapshot vehicle state (DataLayer.snapshot() or demo_snapshot()),
  2. read cached vision status (never raises),
  3. read SoC temperature,
  4. build_state() -> JSON line, broadcast() to all clients,
  5. drain inbound CMD lines and log them (wiring them back into rubyhud is a
     FUTURE step; v1 only logs).

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
from .state import build_state

_LOG = "/tmp/rubysat.log"
# Vision status drop written by rubyvision.
VISION_STATUS_PATH = "/dev/shm/rubyvision/status.json"
# Heartbeat floor: publish at least this often even if --hz is lower or a
# snapshot read stalls, so the Qualia's link-alive timer never trips on a
# healthy-but-quiet bus.
HEARTBEAT_HZ = 2.0
SOC_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"


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

    snapshot_fn, stop_fn = _make_snapshot_source(args.channel, args.novehicle)
    vision = _VisionCache()

    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    seq = 0
    last_emit = 0.0
    try:
        while not stop["flag"]:
            now = time.monotonic()
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
                line = json.dumps(state, separators=(",", ":"))
            except Exception as exc:
                _log("build_state failed: %s" % exc)
                last_emit = now
                continue

            server.broadcast(line)
            seq += 1
            last_emit = now

            # Drain + log inbound CMD lines (FUTURE: route into rubyhud).
            for cmd in server.commands():
                _log("CMD %s" % json.dumps(cmd, separators=(",", ":")))
    finally:
        _log("rubysat shutting down")
        try:
            server.stop()
        except Exception:
            pass
        try:
            stop_fn()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
