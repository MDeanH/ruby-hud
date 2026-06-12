"""rubyhud data layer.

Owns the single data handoff to the renderer: the Snapshot dataclass.
Reads CAN frames off a socketcan channel in a background thread, decodes the
SIM signal set (shared with simdrive), and exposes a thread-safe snapshot().

All system interaction (subprocess, sysfs) is timeout-guarded and never raises
out of a public method or the reader thread.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Snapshot (the ONLY data handoff from data layer to renderer)
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    speed_mph: float | None        # None => show '--'
    rpm: float | None
    gear: str                      # one of 'N','R','P','1'..'6','D','-'
    coolant_c: float | None
    volts: float | None
    throttle_pct: float | None
    fuel_pct: float | None
    source: str                    # 'SIM' | 'LIVE' | 'NO DATA'
    can_fps: int
    can_bus_state: str             # 'UP' | 'NO BUS' | 'ERROR'
    can_listen_only: bool
    cpu_temp_c: float | None
    tailscale: str                 # short, e.g. 'up' / 'down'
    clock: str                     # 'HH:MM'
    warnings: list = field(default_factory=list)  # list[str]
    # raw-bus views for the CAN page (copies; safe to hold across frames)
    recent_frames: list = field(default_factory=list)  # [(mono ts, id, bytes)]
    id_stats: dict = field(default_factory=dict)       # id -> (count, ema_hz)
    total_frames: int = 0


# --------------------------------------------------------------------------- #
# SIM signal encoding (shared with simdrive)
# --------------------------------------------------------------------------- #
# CAN IDs
ID_RPM = 0x201    # bytes 0-1: u16 BE rpm
ID_SPEED = 0x202  # bytes 0-1: u16 BE (speed_mph * 10)
ID_TEMP = 0x203   # byte 0: coolant raw, raw = coolant_C + 40 (offset -40)
ID_AUX = 0x204    # byte0: volts*10, byte1: throttle%, byte2: gear-code, byte3: fuel%

SIM_IDS = (ID_RPM, ID_SPEED, ID_TEMP, ID_AUX)

# ---- Real 2017 MX-5 ND CAN IDs (reverse-engineered on the car) ----
MX5_ID_RPM = 0x202     # b0-1 BE /4 = rpm; b4 = throttle % (verified on car)
MX5_ID_COOLANT = 0x488 # byte 2, raw-40 = coolant degC (best-fit, verify on dash)

# If no frame decodes for this long, vehicle data is considered stale: source
# drops to 'NO DATA' and the live-looking fields blank to None ('--').
STALE_AFTER_S = 1.5

# gear-code map: 0=N,1..6,7=R,8=P,9=D
_GEAR_CODE_TO_STR = {0: "N", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
                     7: "R", 8: "P", 9: "D"}
_GEAR_STR_TO_CODE = {v: k for k, v in _GEAR_CODE_TO_STR.items()}


def _clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---- encoders (used by simdrive) ----
def encode_rpm(rpm: float) -> bytes:
    raw = _clamp(int(round(rpm)), 0, 0xFFFF)
    return bytes([(raw >> 8) & 0xFF, raw & 0xFF])


def encode_speed(speed_mph: float) -> bytes:
    raw = _clamp(int(round(speed_mph * 10)), 0, 0xFFFF)
    return bytes([(raw >> 8) & 0xFF, raw & 0xFF])


def encode_temp(coolant_c: float) -> bytes:
    raw = _clamp(int(round(coolant_c + 40)), 0, 0xFF)
    return bytes([raw])


def encode_aux(volts: float, throttle_pct: float, gear: str, fuel_pct: float) -> bytes:
    v = _clamp(int(round(volts * 10)), 0, 0xFF)
    thr = _clamp(int(round(throttle_pct)), 0, 0xFF)
    code = _GEAR_STR_TO_CODE.get(gear, 0)
    fuel = _clamp(int(round(fuel_pct)), 0, 0xFF)
    return bytes([v, thr, code, fuel])


# ---- decoders (used by DataLayer) ----
def decode_rpm(data: bytes) -> float | None:
    if len(data) < 2:
        return None
    return float((data[0] << 8) | data[1])


def decode_speed(data: bytes) -> float | None:
    if len(data) < 2:
        return None
    return ((data[0] << 8) | data[1]) / 10.0


def decode_temp(data: bytes) -> float | None:
    if len(data) < 1:
        return None
    return float(data[0]) - 40.0


def decode_aux(data: bytes) -> dict | None:
    if len(data) < 4:
        return None
    return {
        "volts": data[0] / 10.0,
        "throttle_pct": float(data[1]),
        "gear": _GEAR_CODE_TO_STR.get(data[2], "-"),
        "fuel_pct": float(data[3]),
    }


# --------------------------------------------------------------------------- #
# Small timeout-guarded helpers (never raise)
# --------------------------------------------------------------------------- #
def _read_text(path: str) -> str | None:
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except Exception:
        return None


def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
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


# --------------------------------------------------------------------------- #
# DataLayer
# --------------------------------------------------------------------------- #
class DataLayer:
    def __init__(self, channel: str):
        self.channel = channel
        self._live = (channel != "vcan0")  # real car vs bench sim decode map
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bus = None

        # vehicle state (last good values)
        self._speed_mph: float | None = None
        self._rpm: float | None = None
        self._gear: str = "-"
        self._coolant_c: float | None = None
        self._volts: float | None = None
        self._throttle_pct: float | None = None
        self._fuel_pct: float | None = None
        self._source = "NO DATA"
        # monotonic time of last successfully-decoded frame (None => never)
        self._last_frame_ts: float | None = None

        # fps (rolling 1s bucket)
        self._frame_count = 0
        self._fps_bucket_start = time.monotonic()
        self._fps = 0

        # listen-only cache
        self._listen_only = False
        self._listen_only_ts = 0.0

        # tailscale cache
        self._tailscale = "down"
        self._tailscale_ts = 0.0

        # raw-bus views for the CAN page (bounded; updated under _lock)
        self.recent: deque = deque(maxlen=24)  # (mono ts, id, data bytes)
        self.id_stats: dict = {}               # id -> [count, last_ts, ema_hz]
        self._total_frames = 0

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="rubyhud-can", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
        self._thread = None
        self._close_bus()

    def _close_bus(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    # -- reader thread ---------------------------------------------------- #
    def _run_loop(self) -> None:
        """Open the bus and read frames forever. Never raises out."""
        last_open_attempt = 0.0
        while not self._stop.is_set():
            if self._bus is None:
                now = time.monotonic()
                # retry bus open every 5s
                if now - last_open_attempt < 5.0:
                    self._stop.wait(0.25)
                    continue
                last_open_attempt = now
                if not self._open_bus():
                    with self._lock:
                        self._source = "NO DATA"
                    self._reset_fps()
                    continue

            try:
                msg = self._bus.recv(timeout=0.5)
            except Exception:
                # bus error -> drop it and reconnect on next loop
                self._close_bus()
                self._reset_fps()
                continue

            if msg is not None:
                try:
                    self._decode(msg)
                except Exception:
                    pass

            self._tick_fps()

        self._close_bus()

    def _open_bus(self) -> bool:
        try:
            import can  # imported lazily so module import never hard-depends on it
            self._bus = can.interface.Bus(
                channel=self.channel, interface="socketcan"
            )
            return True
        except Exception:
            self._bus = None
            return False

    def _tick_fps(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._fps_bucket_start >= 1.0:
                self._fps = self._frame_count
                self._frame_count = 0
                self._fps_bucket_start = now

    def _reset_fps(self) -> None:
        """Age fps to 0 when the bus is unavailable so a dead bus never keeps
        reporting the last healthy frame rate."""
        now = time.monotonic()
        with self._lock:
            self._fps = 0
            self._frame_count = 0
            self._fps_bucket_start = now

    def _decode(self, msg) -> None:
        arb = getattr(msg, "arbitration_id", None)
        data = bytes(getattr(msg, "data", b""))
        with self._lock:
            self._frame_count += 1
            self._last_frame_ts = time.monotonic()
            self._record_raw(arb, data, self._last_frame_ts)

            # Real car (can0) and the vcan0 simulator collide on some IDs
            # (e.g. 0x202 = sim SPEED but real MX-5 RPM). Pick the map by
            # channel so the bench sim and the live car never cross-decode.
            if self._live:
                self._decode_mx5(arb, data)
            elif arb in SIM_IDS:
                self._source = "SIM"
                if arb == ID_RPM:
                    v = decode_rpm(data)
                    if v is not None:
                        self._rpm = v
                elif arb == ID_SPEED:
                    v = decode_speed(data)
                    if v is not None:
                        self._speed_mph = v
                elif arb == ID_TEMP:
                    v = decode_temp(data)
                    if v is not None:
                        self._coolant_c = v
                elif arb == ID_AUX:
                    aux = decode_aux(data)
                    if aux is not None:
                        self._volts = aux["volts"]
                        self._throttle_pct = aux["throttle_pct"]
                        self._gear = aux["gear"]
                        self._fuel_pct = aux["fuel_pct"]
            else:
                # vcan0 sim, unmapped sim ID: leave fields untouched.
                self._source = "SIM"

    def _decode_mx5(self, arb, data: bytes) -> None:
        """2017 MX-5 ND live decode map (reverse-engineered on the car,
        2026-06-13). Caller holds _lock. Add signals here as verified."""
        self._source = "LIVE"
        # RPM: ID 0x202, bytes 0-1 BE / 4. Verified by rev test: idle raw
        # 3291 (=822 rpm), ~3k rev raw 11635 (=2908 rpm).
        if arb == MX5_ID_RPM and len(data) >= 5:
            self._rpm = float((data[0] << 8) | data[1]) / 4.0
            # Throttle/accelerator: byte 4 = 0-100% (idle 0; pedal-press scan
            # showed pair b4-5 = b4*256, i.e. b4 is the integer percent).
            self._throttle_pct = float(data[4])
        # Coolant: ID 0x488 byte 2, raw - 40 = degC (warm raw 0x82=130 -> 90C).
        elif arb == MX5_ID_COOLANT and len(data) >= 3:
            self._coolant_c = float(data[2]) - 40.0
        # Speed (MPH): not yet identified (car parked during rev test) ->
        # leave None so it shows '--' rather than a bogus number.

    def _record_raw(self, arb, data: bytes, now: float) -> None:
        """Track raw traffic for the CAN page. Caller holds _lock."""
        self._total_frames += 1
        if not isinstance(arb, int):
            return
        self.recent.append((now, arb, data))
        st = self.id_stats.get(arb)
        if st is None:
            if len(self.id_stats) >= 128:
                # Evict the least-seen id to keep the table bounded.
                evict = min(self.id_stats, key=lambda k: self.id_stats[k][0])
                del self.id_stats[evict]
            self.id_stats[arb] = [1, now, 0.0]
            return
        st[0] += 1
        dt = now - st[1]
        st[1] = now
        if dt > 0:
            inst = 1.0 / dt
            st[2] = inst if st[2] <= 0 else st[2] * 0.8 + inst * 0.2

    # -- bus state helpers ------------------------------------------------ #
    def _bus_state(self) -> str:
        oper = _read_text("/sys/class/net/%s/operstate" % self.channel)
        if oper is None:
            return "NO BUS"
        can_state = _read_text("/sys/class/net/%s/can/state" % self.channel)
        if can_state is not None:
            cs = can_state.lower()
            if "error" in cs or "bus-off" in cs:
                return "ERROR"
        if oper.lower() in ("up", "unknown"):
            return "UP"
        return "NO BUS"

    def _read_listen_only(self) -> bool:
        now = time.monotonic()
        if now - self._listen_only_ts < 5.0:
            return self._listen_only
        self._listen_only_ts = now
        out = _run(["ip", "-details", "link", "show", self.channel], timeout=2.0)
        if out is not None:
            self._listen_only = "<LISTEN-ONLY>" in out or "listen-only on" in out
        return self._listen_only

    def _read_cpu_temp(self) -> float | None:
        raw = _read_text("/sys/class/thermal/thermal_zone0/temp")
        if raw is None:
            return None
        try:
            return int(raw) / 1000.0
        except Exception:
            return None

    def _read_tailscale(self) -> str:
        now = time.monotonic()
        if now - self._tailscale_ts < 10.0:
            return self._tailscale
        self._tailscale_ts = now
        out = _run(["systemctl", "is-active", "tailscaled"], timeout=2.0)
        if out is not None and out.strip() == "active":
            self._tailscale = "up"
        else:
            self._tailscale = "down"
        return self._tailscale

    # -- snapshot --------------------------------------------------------- #
    def snapshot(self) -> Snapshot:
        bus_state = self._bus_state()
        listen_only = self._read_listen_only()
        cpu_temp = self._read_cpu_temp()
        tailscale = self._read_tailscale()
        clock = time.strftime("%H:%M", time.localtime())

        with self._lock:
            last_ts = self._last_frame_ts
            stale = (last_ts is None
                     or (time.monotonic() - last_ts) > STALE_AFTER_S)
            if stale:
                # Frame drought (sim killed, ECU asleep, harness unplugged):
                # blank vehicle data instead of showing a frozen live-looking
                # reading, and report no source.
                speed = None
                rpm = None
                gear = "-"
                coolant = None
                volts = None
                throttle = None
                fuel = None
                source = "NO DATA"
            else:
                speed = self._speed_mph
                rpm = self._rpm
                gear = self._gear
                coolant = self._coolant_c
                volts = self._volts
                throttle = self._throttle_pct
                fuel = self._fuel_pct
                source = self._source
            fps = self._fps
            recent = list(self.recent)
            id_stats = {k: (v[0], v[2]) for k, v in self.id_stats.items()}
            total_frames = self._total_frames

        warnings: list[str] = []
        if coolant is not None and coolant > 110:
            warnings.append("COOLANT HOT")
        if volts is not None and volts < 11.8:
            warnings.append("LOW VOLTS")
        # can_bus_state != 'UP' -> no warning, just chip rendered elsewhere

        return Snapshot(
            speed_mph=speed,
            rpm=rpm,
            gear=gear,
            coolant_c=coolant,
            volts=volts,
            throttle_pct=throttle,
            fuel_pct=fuel,
            source=source,
            can_fps=int(fps),
            can_bus_state=bus_state,
            can_listen_only=bool(listen_only),
            cpu_temp_c=cpu_temp,
            tailscale=tailscale,
            clock=clock,
            warnings=warnings,
            recent_frames=recent,
            id_stats=id_stats,
            total_frames=total_frames,
        )

    # -- demo ------------------------------------------------------------- #
    @staticmethod
    def demo_snapshot() -> Snapshot:
        now = time.monotonic()
        demo_recent = [
            (now - 0.520, ID_AUX, encode_aux(14.1, 38, "4", 64)),
            (now - 0.410, ID_TEMP, encode_temp(92)),
            (now - 0.310, ID_SPEED, encode_speed(61.8)),
            (now - 0.210, ID_RPM, encode_rpm(4180)),
            (now - 0.110, ID_SPEED, encode_speed(62.0)),
            (now - 0.010, ID_RPM, encode_rpm(4200)),
        ]
        demo_stats = {
            ID_RPM: (4210, 50.1),
            ID_SPEED: (4205, 49.8),
            ID_TEMP: (842, 10.0),
            ID_AUX: (840, 10.0),
        }
        return Snapshot(
            speed_mph=62,
            rpm=4200,
            gear="4",
            coolant_c=92,
            volts=14.1,
            throttle_pct=38,
            fuel_pct=64,
            source="SIM",
            can_fps=480,
            can_bus_state="UP",
            can_listen_only=True,
            cpu_temp_c=48.0,
            tailscale="up",
            clock="14:30",
            warnings=[],
            recent_frames=demo_recent,
            id_stats=demo_stats,
            total_frames=10097,
        )
