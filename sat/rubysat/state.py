"""build_state -- map (Snapshot, vision status, soc temp) -> the STATE dict.

The STATE line schema (Ruby -> Qualia, newline-delimited JSON, ASCII only):

  {"t":<unix float>,"seq":<int>,"rpm":<int|null>,"mph":<int|null>,
   "gear":"<str>","coolant":<int|null>,"volts":<float|null>,
   "throttle":<int|null>,"fuel":<int|null>,"bus":"<UP|NO BUS|ERROR>",
   "canfps":<int>,"vsrc":"<csi|usb|video|pattern|off>","vdets":<int>,
   "soc":<float|null>}

Rules:
  * None passes through as JSON null for every nullable numeric field.
  * rpm, mph, coolant, throttle, fuel -> int (rounded) or null.
  * volts -> float rounded to 1 decimal, or null.
  * soc -> float rounded to 1 decimal, or null.
  * gear -> str (Snapshot.gear is already 'N','R','1'..'6','D','-').
  * bus  -> Snapshot.can_bus_state, already one of UP / NO BUS / ERROR. Any
            unexpected value is coerced to NO BUS so the Qualia only ever sees
            the three documented states.
  * vsrc/vdets come from the vision status dict; if it's missing or stale
    (older than VISION_STALE_S) they fall back to "off" / 0.

Nothing here raises: a malformed snapshot or vision dict must not break the
publish loop. Every field is defensively coerced.
"""

from __future__ import annotations

import re
import time

# Vision status older than this is considered stale -> vsrc "off", vdets 0.
VISION_STALE_S = 2.0

_BUS_OK = ("UP", "NO BUS", "ERROR")
_VSRC_OK = ("csi", "usb", "video", "pattern")


def _opt_int(value):
    """Round to int, or None. Never raises (bad value -> None)."""
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _opt_round1(value):
    """Round to 1 decimal float, or None. Never raises."""
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _gear_str(value) -> str:
    if value is None:
        return "-"
    try:
        s = str(value)
    except Exception:
        return "-"
    # Keep it ASCII and short; the gear codes are already single chars.
    return s if s else "-"


def _bus_state(value) -> str:
    try:
        s = str(value)
    except Exception:
        return "NO BUS"
    return s if s in _BUS_OK else "NO BUS"


def _vision_fields(vision_status, now: float):
    """Return (vsrc, vdets) from a vision status dict, honoring staleness.

    Accepts the rubyvision status schema: {"ts": <unix float>,
    "source": "<csi|usb|video|pattern>", "detections": [...]}. Missing, stale,
    or malformed -> ("off", 0)."""
    if not isinstance(vision_status, dict):
        return "off", 0

    ts = vision_status.get("ts")
    try:
        if ts is None or (now - float(ts)) > VISION_STALE_S:
            return "off", 0
    except (TypeError, ValueError):
        return "off", 0

    # rubyvision reports source as src.name (sources.py). csi/video/pattern are
    # canonical, but the USB source name carries the device index: "usb%d" %
    # idx -> "usb0"/"usb1" (sources.py UvcSource). Strip a trailing numeric
    # index before the membership check so a live USB camera maps to "usb"
    # rather than "off" (which would contradict a non-zero vdets on the Qualia).
    src = vision_status.get("source")
    base = re.sub(r"\d+$", "", src) if isinstance(src, str) else ""
    vsrc = base if base in _VSRC_OK else "off"

    dets = vision_status.get("detections")
    if isinstance(dets, list):
        vdets = len(dets)
    else:
        vdets = _opt_int(dets) or 0
    if vdets < 0:
        vdets = 0
    return vsrc, vdets


def build_state(snapshot, vision_status, soc_temp, seq: int, t: float) -> dict:
    """Build a STATE dict exactly matching the wire schema. Never raises.

    Args:
        snapshot: rubyhud signals.Snapshot (or a demo one) -- read by attribute.
        vision_status: dict from /dev/shm/rubyvision/status.json, or None.
        soc_temp: float SoC temperature in C, or None.
        seq: monotonically increasing sequence int.
        t: unix timestamp (float) for this record.
    """
    # Pull fields defensively: a partial/None snapshot must not crash the loop.
    g = getattr  # local alias

    rpm = _opt_int(g(snapshot, "rpm", None))
    mph = _opt_int(g(snapshot, "speed_mph", None))
    gear = _gear_str(g(snapshot, "gear", "-"))
    coolant = _opt_int(g(snapshot, "coolant_c", None))
    volts = _opt_round1(g(snapshot, "volts", None))
    throttle = _opt_int(g(snapshot, "throttle_pct", None))
    fuel = _opt_int(g(snapshot, "fuel_pct", None))
    bus = _bus_state(g(snapshot, "can_bus_state", "NO BUS"))
    canfps = _opt_int(g(snapshot, "can_fps", 0)) or 0
    if canfps < 0:
        canfps = 0

    vsrc, vdets = _vision_fields(vision_status, t)
    soc = _opt_round1(soc_temp)

    return {
        "t": round(float(t), 3),
        "seq": int(seq),
        "rpm": rpm,
        "mph": mph,
        "gear": gear,
        "coolant": coolant,
        "volts": volts,
        "throttle": throttle,
        "fuel": fuel,
        "bus": bus,
        "canfps": canfps,
        "vsrc": vsrc,
        "vdets": vdets,
        "soc": soc,
    }
