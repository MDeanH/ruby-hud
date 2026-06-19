"""Stream the 480x480 satellite view to the Qualia thin client over USB-serial.

Renders rubyhud.satframe from a live Snapshot at ~N fps and writes each JPEG to
the Qualia as a framed packet; reads touch packets back. The Qualia firmware
(qualia/satdisplay) just decodes + blits, so the 4" shares the 7"'s look and
updates via a normal Pi OTA -- this driver is the Pi half of that pipeline.

Wire protocol:
  Pi  -> Qualia : 0xA5 0x5A  <len:u32 LE>  <jpeg bytes>
  Qualia -> Pi  : 0x54 0x43  <x:u16 LE>    <y:u16 LE>     (480-space touch)

Run:  python -m rubyhud.satdriver --port /dev/ttyACM0 [--fps 12] [--demo]
"""

from __future__ import annotations

import argparse
import struct
import sys
import time


def _log(msg):
    sys.stderr.write("satdriver: %s\n" % msg)
    sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--quality", type=int, default=72)
    ap.add_argument("--demo", action="store_true",
                    help="stream the demo snapshot (test without CAN)")
    ap.add_argument("--test", action="store_true",
                    help="stream a fixed R/G/B/W color test card (bring-up only)")
    ap.add_argument("--sweep", action="store_true",
                    help="demo: sweep rpm/speed so the shift light engages live")
    args = ap.parse_args()

    import serial
    from . import satframe

    if args.test:
        card = _test_card(args.quality)
        snap_fn = None
    elif args.sweep:
        snap_fn = _sweep_snapshot()
    else:
        from .signals import DataLayer
        if args.demo:
            snap_fn = DataLayer.demo_snapshot
        else:
            dl = DataLayer(args.channel)
            dl.start()
            snap_fn = dl.snapshot

    ser = serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=2.0)
    _log("request-driven stream -> %s (cap %.0f fps)" % (args.port, args.fps))
    min_period = 1.0 / max(1.0, args.fps)
    rxbuf = bytearray()
    state = {"n": 0, "ser": ser, "last": 0.0}

    def _send():
        # Reset the keepalive timer up front so a write that raises below still
        # counts as "just attempted" -- otherwise a failed request-driven send
        # leaves last stale and the keepalive double-sends in the same loop.
        state["last"] = time.monotonic()
        jpg = card if args.test else satframe.jpeg(snap_fn(), quality=args.quality)
        state["ser"].write(b"\xA5\x5A" + struct.pack("<I", len(jpg)) + jpg)
        state["n"] += 1
        if state["n"] % 60 == 0:
            _log("%d frames, last %d B" % (state["n"], len(jpg)))

    # The Qualia drives the pace: it sends 0x52 ('R') when it's ready for the
    # next frame (after decoding+blitting the previous one), so we never overrun
    # its tiny USB-CDC buffer. A keepalive covers startup / a lost request.
    while True:
        try:
            chunk = state["ser"].read(64)
        except serial.SerialException as exc:
            _log("read failed (%s); reopening" % exc)
            time.sleep(0.5)
            try:
                state["ser"].close()
                state["ser"] = serial.Serial(args.port, args.baud,
                                             timeout=0.05, write_timeout=2.0)
            except Exception:
                pass
            continue
        if chunk:
            rxbuf.extend(chunk)
        while rxbuf:
            b = rxbuf[0]
            if b == 0x52:                       # ready -> send next frame
                del rxbuf[:1]
                if time.monotonic() - state["last"] >= min_period:
                    try:
                        _send()
                    except serial.SerialException as exc:
                        _log("write failed (%s)" % exc)
            elif b == 0x54:                     # touch: 0x54 0x43 x:u16 y:u16
                if len(rxbuf) < 6:
                    break
                if rxbuf[1] == 0x43:
                    _on_touch(rxbuf[2] | (rxbuf[3] << 8),
                              rxbuf[4] | (rxbuf[5] << 8))
                del rxbuf[:6]
            else:
                del rxbuf[:1]                   # resync
        if time.monotonic() - state["last"] > 1.0:   # keepalive
            try:
                _send()
            except serial.SerialException as exc:
                _log("keepalive write failed (%s)" % exc)


def _sweep_snapshot():
    """Return a snap_fn that triangle-sweeps rpm 1000->7600->1000 (~8s) with
    speed + gear tracking, so the shift light visibly engages and releases on
    the panel. Demo/bench only."""
    import dataclasses
    import time

    from .signals import DataLayer

    base = DataLayer.demo_snapshot()
    t0 = time.monotonic()

    def snap_fn():
        ph = ((time.monotonic() - t0) / 8.0) % 1.0
        tri = 2.0 * ph if ph < 0.5 else 2.0 * (1.0 - ph)   # 0..1..0
        rpm = 1000.0 + tri * 6600.0
        spd = 10.0 + tri * 110.0
        gear = str(min(6, max(1, int(rpm // 1300) + 1)))
        return dataclasses.replace(base, rpm=rpm, speed_mph=spd,
                                   gear=gear, source="SIM")

    return snap_fn


def _test_card(quality: int = 80) -> bytes:
    """A 480x480 labeled color card for one-shot color-order verification.

    Quadrants are pure primaries with their NAME printed on them, so a single
    photo of the panel tells us the exact channel mapping: if the tile labeled
    "RED" looks blue, R/B are swapped; speckled/noisy tiles mean the JPEG byte
    order (RGB565 endianness) is wrong, not the channel order.
    """
    import io

    from PIL import Image, ImageDraw

    from .theme import font

    img = Image.new("RGB", (480, 480), (0, 0, 0))
    d = ImageDraw.Draw(img)
    quads = (((0, 0), (255, 0, 0), "RED"),
             ((240, 0), (0, 255, 0), "GREEN"),
             ((0, 240), (0, 0, 255), "BLUE"),
             ((240, 240), (255, 255, 255), "WHITE"))
    for (ox, oy), col, name in quads:
        d.rectangle([ox, oy, ox + 239, oy + 239], fill=col)
        tc = (0, 0, 0) if name == "WHITE" else (255, 255, 255)
        d.text((ox + 120, oy + 120), name, font=font(34, "bold"),
               fill=tc, anchor="mm")
    # centre marker so we can confirm geometry/orientation too
    d.ellipse([220, 220, 260, 260], outline=(255, 255, 0), width=4)
    d.text((240, 462), "RUBY COLOR TEST", font=font(20, "bold"),
           fill=(255, 255, 0), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _on_touch(x, y):
    # Phase 4: route the 480-space tap into the satellite UI (page cycle, etc.).
    _log("touch %d,%d" % (x, y))


if __name__ == "__main__":
    main()
