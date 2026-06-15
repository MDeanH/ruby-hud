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
    # ---- extra ND1 RF live signals (None/'-'/False when stale/unavailable) --
    ambient_c: float | None = None     # outside-air temp (0x420 b6-7)
    map_kpa: float | None = None       # manifold abs pressure (0xFD b7)
    roof: str = "-"                    # 'CLOSED'|'OPENING'|'OPEN'|'CLOSING' (RF, 0x472)
    turn: str = "off"                 # 'off'|'L'|'R'|'LR'
    headlight: str = "off"            # 'off'|'TNS'|'TNS_LO'|'HI'
    parking_brake: bool = False
    reverse: bool = False
    # ---- body / safety indicators (0x43E doors+trunk, 0x47B BSM) -----------
    door_left: bool = False     # driver-side door ajar (DBC DoorLeft)
    door_right: bool = False    # passenger-side door ajar (DBC DoorRight)
    trunk: bool = False         # trunk/boot open (DBC Trunk)
    bsm_left: bool = False      # vehicle in left blind spot (DBC BSM_Left)
    bsm_right: bool = False     # vehicle in right blind spot (DBC BSM_Right)
    bsm_warning: bool = False   # escalated BSM warning flag (DBC BSM_Warning)


# --------------------------------------------------------------------------- #
# SIM signal encoding (shared with simdrive)
# --------------------------------------------------------------------------- #
# CAN IDs
ID_RPM = 0x201    # bytes 0-1: u16 BE rpm
ID_SPEED = 0x202  # bytes 0-1: u16 BE (speed_mph * 10)
ID_TEMP = 0x203   # byte 0: coolant raw, raw = coolant_C + 40 (offset -40)
ID_AUX = 0x204    # byte0: volts*10, byte1: throttle%, byte2: gear-code, byte3: fuel%

SIM_IDS = (ID_RPM, ID_SPEED, ID_TEMP, ID_AUX)

# ---- Real 2017 ND1 MX-5 GT RF CAN IDs (reverse-engineered on the car) ----
# IDs/signals from the community ND DBC (berumiya/CAN_DBC_6thGenMazda,
# MX5ND_6thGenMazda_HSCAN.dbc). All signals big-endian (Motorola, @0+).
MX5_ID_PCM  = 0x202  # b0-1 rpm*0.25, b2-3 speed km/h*0.01, b4-5 throttle*0.0015625
MX5_ID_TEMP = 0x420  # b0 coolant(-40)C, b6-7 ambient*0.25-3200 C
MX5_ID_FUEL = 0x9E   # b5 fuel *0.2 L (ND tank ~45 L)
MX5_ID_PCM2 = 0xFD   # MT_Gear_Actual @bit19 len3; MAP @b7 (kPa, +2 offset)
MX5_ID_BCMM = 0x9A   # Turn @bit19 len2 (1=L,2=R,3=LR); Headlight @bit7 len4
MX5_ID_IC   = 0x9F   # Parking_Brake @bit4; Reverse_Flag @bit7
MX5_ID_ROOF = 0x472  # RoofGraphicStatus @bit23 len4 (RF retractable hardtop)
MX5_ID_DOORS = 0x43E  # HS_BCMM (1086): DoorLeft@36, DoorRight@37, Trunk@47 (1-bit)
MX5_ID_BSM  = 0x47B   # HS_IC (1147): BSM_Right@1, BSM_Warning@2, BSM_Left@15 (1-bit)

# Motorola/big-endian @0+ value tables from the DBC.
_TURN_MAP = {0: "off", 1: "L", 2: "R", 3: "LR"}
_HEADLIGHT_MAP = {0: "off", 2: "TNS", 3: "TNS_LO", 12: "HI"}
# RF roof (0x472) calibrated on-car 2026-06-14 by operating the top while logging:
# the DBC's RoofGraphicStatus 4-bit field is only the dash animation. Real state =
# byte2 motion direction (0 idle / 2 opening / 4 closing, +0x8 = blink phase) and,
# when idle, byte1 (0x05 closed / 0x03 open). Decoded inline in _decode_mx5.
# MT_Gear_Actual (3-bit): 0=neutral, 1..6 gears. Reverse comes from 0x9F.
_MT_GEAR_MAP = {0: "N", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6"}


def _moto(data: bytes, start_bit: int, length: int) -> int | None:
    """Extract a Motorola/big-endian (@0+) signal from a CAN payload.

    `start_bit` is in DBC sawtooth notation (the signal's MSB; bits count
    0=LSB..7=MSB within each byte). Returns None if the signal would run past
    the available payload.
    """
    total = len(data) * 8
    byte_idx = start_bit // 8
    bit_in_byte = start_bit % 8
    msb_pos = byte_idx * 8 + (7 - bit_in_byte)   # position from the left
    if byte_idx >= len(data) or msb_pos + length > total:
        return None
    val = int.from_bytes(data, "big")
    shift = total - (msb_pos + length)
    return (val >> shift) & ((1 << length) - 1)

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
        self._ambient_c: float | None = None
        self._map_kpa: float | None = None
        self._roof: str = "-"
        self._door_left: bool = False
        self._door_right: bool = False
        self._trunk: bool = False
        self._bsm_left: bool = False
        self._bsm_right: bool = False
        self._bsm_warning: bool = False
        self._turn: str = "off"
        self._headlight: str = "off"
        self._parking_brake: bool = False
        self._reverse: bool = False
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
        """2017 ND1 MX-5 GT RF live decode map. Signal definitions from the
        community DBC (berumiya/CAN_DBC_6thGenMazda, MX5ND_6thGenMazda_HSCAN.dbc),
        all big-endian (Motorola, @0+). RPM cross-checked on the car (rev test).
        Caller holds _lock.

        NOTE: oil temp and battery voltage are NOT broadcast on HS-CAN (the
        Sport-gauge oil temp is computed inside the cluster) — unavailable.
        """
        self._source = "LIVE"
        # ---- 0x202 HS_PCM: RPM + vehicle speed + accelerator ----
        if arb == MX5_ID_PCM and len(data) >= 6:
            self._rpm = float((data[0] << 8) | data[1]) * 0.25      # b0-1 *0.25
            kmh = float((data[2] << 8) | data[3]) * 0.01            # b2-3 *0.01
            self._speed_mph = kmh * 0.621371
            self._throttle_pct = float((data[4] << 8) | data[5]) * 0.0015625
        # ---- 0x420 HS_PCM: coolant (b0 -40) ----
        # NOTE: the DBC's AmbientTemp (b6-7 *0.25 -3200) reads a constant on the
        # ND1 (b6-7 = F2 9B, a counter/checksum, not outside-air temp), so it is
        # NOT decoded — ambient is cluster-only, like oil temp / battery volts.
        elif arb == MX5_ID_TEMP and len(data) >= 1:
            self._coolant_c = float(data[0]) - 40.0
        # ---- 0x9E HS_IC: fuel tank (b5 *0.25 L -> % of 45 L ND tank) ----
        # Scale calibrated on the car 2026-06-15: with a FULL tank, byte5=0xB5=181;
        # 181*0.25 = 45.25 L ~= the 45 L ND tank, so full reads 100%. The community
        # DBC's 0.2 L/bit under-read (a full tank showed 80%). Single-point (full)
        # calibration; sender assumed linear through 0 -- refine with a low-fuel
        # reading later (note byte5 when the factory low-fuel light triggers).
        elif arb == MX5_ID_FUEL and len(data) >= 6:
            liters = float(data[5]) * 0.25
            self._fuel_pct = max(0.0, min(100.0, liters / 45.0 * 100.0))
        # ---- 0xFD HS_PCM: MT gear (@bit19 len3) + MAP (b6, +2 kPa offset) ----
        elif arb == MX5_ID_PCM2 and len(data) >= 7:
            g = _moto(data, 19, 3)
            if g is not None and not self._reverse:
                self._gear = _MT_GEAR_MAP.get(g, "-")
            self._map_kpa = float(data[6]) + 2.0
        # ---- 0x9A HS_BCMM: turn signals (@bit19 len2) + headlights (@b7 len4)-
        elif arb == MX5_ID_BCMM and len(data) >= 3:
            t = _moto(data, 19, 2)
            if t is not None:
                self._turn = _TURN_MAP.get(t, "off")
            h = _moto(data, 7, 4)
            if h is not None:
                self._headlight = _HEADLIGHT_MAP.get(h, "off")
        # ---- 0x9F HS_IC: parking brake (@bit4) + reverse flag (@bit7) --------
        elif arb == MX5_ID_IC and len(data) >= 1:
            self._parking_brake = bool((data[0] >> 4) & 1)
            self._reverse = bool((data[0] >> 7) & 1)
            self._gear = "R" if self._reverse else self._gear
        # ---- 0x472 HS_RHT: RF retractable hardtop status (calibrated on-car) -
        # byte2 high-nibble = motion (0 idle, 2 opening, 4 closing; +0x8 blink);
        # when idle, byte1 gives the resting state (0x05 closed, 0x03 open).
        # Ambiguous idle frames keep the last known state.
        elif arb == MX5_ID_ROOF and len(data) >= 3:
            direction = (data[2] >> 4) & 0x7
            if direction == 2:
                self._roof = "OPENING"
            elif direction == 4:
                self._roof = "CLOSING"
            elif data[1] == 0x05:
                self._roof = "CLOSED"
            elif data[1] == 0x03:
                self._roof = "OPEN"
        # ---- 0x43E HS_BCMM: door + trunk ajar (1-bit each, Motorola @0+) -----
        # VAL_: doors 0=Closed/1=Ajar, trunk 0=Closed/1=Open. L/R-to-physical
        # mapping confirmed on the car by opening each door (see can/README).
        elif arb == MX5_ID_DOORS and len(data) >= 6:
            dl = _moto(data, 36, 1)
            dr = _moto(data, 37, 1)
            tk = _moto(data, 47, 1)
            if dl is not None:
                self._door_left = bool(dl)
            if dr is not None:
                self._door_right = bool(dr)
            if tk is not None:
                self._trunk = bool(tk)
        # ---- 0x47B HS_IC: blind-spot monitor (1-bit each, Motorola @0+) ------
        # BSM activates >~19 mph with a vehicle in the adjacent lane; bits read
        # 0 at rest. Positive verification needs a drive test (see can/README).
        elif arb == MX5_ID_BSM and len(data) >= 2:
            br = _moto(data, 1, 1)
            bw = _moto(data, 2, 1)
            bl = _moto(data, 15, 1)
            if br is not None:
                self._bsm_right = bool(br)
            if bw is not None:
                self._bsm_warning = bool(bw)
            if bl is not None:
                self._bsm_left = bool(bl)

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
                ambient = None
                map_kpa = None
                roof = "-"
                turn = "off"
                headlight = "off"
                parking_brake = False
                reverse = False
                door_left = door_right = trunk = False
                bsm_left = bsm_right = bsm_warning = False
                source = "NO DATA"
            else:
                speed = self._speed_mph
                rpm = self._rpm
                gear = self._gear
                coolant = self._coolant_c
                volts = self._volts
                throttle = self._throttle_pct
                fuel = self._fuel_pct
                ambient = self._ambient_c
                map_kpa = self._map_kpa
                roof = self._roof
                turn = self._turn
                headlight = self._headlight
                parking_brake = self._parking_brake
                reverse = self._reverse
                door_left = self._door_left
                door_right = self._door_right
                trunk = self._trunk
                bsm_left = self._bsm_left
                bsm_right = self._bsm_right
                bsm_warning = self._bsm_warning
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
            ambient_c=ambient,
            map_kpa=map_kpa,
            roof=roof,
            turn=turn,
            headlight=headlight,
            parking_brake=parking_brake,
            reverse=reverse,
            door_left=door_left,
            door_right=door_right,
            trunk=trunk,
            bsm_left=bsm_left,
            bsm_right=bsm_right,
            bsm_warning=bsm_warning,
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
