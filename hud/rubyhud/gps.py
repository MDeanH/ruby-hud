"""GPS via gpsd: a background reader exposing the latest fix to the HUD.

Connects to the local gpsd JSON socket (127.0.0.1:2947), watches, and keeps the
most recent TPV (time/position/velocity) + SKY (satellites) in a thread-safe
snapshot. Non-blocking and self-healing (reconnects if gpsd restarts or hasn't
come up yet); never raises into the render path. gpsd itself reads the USB GNSS
on /dev/ttyUSB0 (see deploy: /etc/default/gpsd).

mode: 0/1 = no fix, 2 = 2D, 3 = 3D.
"""

from __future__ import annotations

import json
import socket
import threading
import time

_HOST, _PORT = "127.0.0.1", 2947


class GpsReader:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"mode": 0, "lat": None, "lon": None, "speed_mps": None,
                       "track": None, "sats_used": 0, "sats_seen": 0,
                       "ts": 0.0, "online": False}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                s = socket.create_connection((_HOST, _PORT), timeout=5)
                s.settimeout(10)
                s.sendall(b'?WATCH={"enable":true,"json":true};\n')
                with self._lock:
                    self._state["online"] = True
                f = s.makefile("r")
                for line in f:
                    if self._stop.is_set():
                        break
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    self._ingest(d)
            except Exception:
                pass
            with self._lock:
                self._state["online"] = False
            time.sleep(2.0)   # gpsd down / not up yet -> retry

    def _ingest(self, d):
        cls = d.get("class")
        with self._lock:
            if cls == "TPV":
                self._state["mode"] = int(d.get("mode") or 0)
                for k in ("lat", "lon", "track"):
                    if k in d:
                        self._state[k] = d[k]
                if "speed" in d:
                    self._state["speed_mps"] = d["speed"]
                self._state["ts"] = time.monotonic()
                self._state["online"] = True
            elif cls == "SKY":
                sats = d.get("satellites") or []
                self._state["sats_seen"] = len(sats)
                self._state["sats_used"] = sum(1 for x in sats if x.get("used"))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)


_reader = None


def reader() -> GpsReader:
    """Module-level singleton; starts the background thread on first use."""
    global _reader
    if _reader is None:
        _reader = GpsReader()
        _reader.start()
    return _reader
