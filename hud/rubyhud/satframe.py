"""480x480 satellite view for the 4" Qualia, rendered ON THE PI in the same
Tesla language as the 7" HUD.

The Pi renders BOTH screens from one Pillow codebase: render(snap) returns a
480x480 RGB image (condensed gauge cluster), jpeg(snap) returns JPEG bytes. The
frame is streamed to the Qualia thin client over USB-serial, so the 4" shares
the 7"'s exact look and every UI change ships via a normal Pi OTA -- the Qualia
firmware (a dumb JPEG-blit client) is flashed once and never touched again.

Square layout: a 270-degree rpm arc (redline segment) wrapping a hero speed
numeral, gear at the top, coolant + fuel along the bottom. Pure near-black,
hairlines, thin numerals -- the same vocabulary as render.py / GaugesPage.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from . import config, gauges
from .theme import BG, CARD_BORDER, DANGER, OK, TEXT, TEXT_DIM, WARN, font, mix

SAT = 480                      # logical panel size (square)
SS = 2                         # supersample; render at 960 then downsample
W = SAT * SS

RPM_MAX = 8000.0
REDLINE = 7000.0
COOLANT_LO, COOLANT_HI = 40.0, 120.0

# arc geometry (SS px)
CX = CY = W // 2
ARC_R = 408
ARC_A0, ARC_SWEEP = 135.0, 270.0


def _num(value):
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    return None if (v != v or v in (float("inf"), float("-inf"))) else value


def _frac(value, lo, hi):
    v = _num(value)
    if v is None or hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (float(v) - lo) / (hi - lo)))


def render(snap) -> Image.Image:
    """Render the 480x480 satellite view for `snap`."""
    img = Image.new("RGB", (W, W), tuple(BG))
    draw = ImageDraw.Draw(img)
    a1 = ARC_A0 + ARC_SWEEP
    rl0 = ARC_A0 + (REDLINE / RPM_MAX) * ARC_SWEEP

    # ---- rpm arc (track + redline + live) --------------------------------
    gauges.arc_seg(draw, CX, CY, ARC_R, ARC_A0, a1,
                   mix(BG, TEXT_DIM, 0.30), 3 * SS)
    gauges.arc_seg(draw, CX, CY, ARC_R, rl0, a1, DANGER, 4 * SS)
    rpm = _num(snap.rpm)
    in_red = rpm is not None and float(rpm) >= REDLINE
    if rpm is not None and float(rpm) > 0:
        ra1 = ARC_A0 + min(1.0, float(rpm) / RPM_MAX) * ARC_SWEEP
        gauges.arc_seg(draw, CX, CY, ARC_R, ARC_A0, ra1,
                       DANGER if in_red else TEXT, 4 * SS)
        dx, dy = gauges._polar(CX, CY, ARC_R, ra1)
        draw.ellipse([dx - 5 * SS, dy - 5 * SS, dx + 5 * SS, dy + 5 * SS],
                     fill=DANGER if in_red else TEXT)
    # tick labels 0..8
    tf = font(15 * SS, "regular")
    for i, lab in enumerate(("0", "2", "4", "6", "8")):
        px, py = gauges._polar(CX, CY, ARC_R - 34 * SS,
                               ARC_A0 + (i / 4.0) * ARC_SWEEP)
        gauges._centered_text(draw, px, py, lab, tf, mix(BG, TEXT_DIM, 0.55))

    # ---- top: live dot + gear -------------------------------------------
    src = (snap.source or "NO DATA")
    dotcol = OK if src == "LIVE" else (WARN if src == "SIM" else DANGER)
    draw.ellipse([CX - 70 * SS - 5 * SS, 64 * SS - 5 * SS,
                  CX - 70 * SS + 5 * SS, 64 * SS + 5 * SS], fill=dotcol)
    gauges.tracked_text_center(draw, CX, 64 * SS, "RUBY",
                               font(20 * SS, "bold"), TEXT, tracking=5 * SS)
    g = snap.gear or "-"
    draw.text((CX, 128 * SS), g if g != "-" else "–",
              font=font(58 * SS, "thin"), fill=TEXT, anchor="mm")

    # ---- hero speed ------------------------------------------------------
    spd = _num(snap.speed_mph)
    s = "--" if spd is None else "%d" % int(round(config.mph_to_disp(float(spd))))
    draw.text((CX, CY + 6 * SS), s, font=font(150 * SS, "thin"),
              fill=DANGER if in_red else TEXT, anchor="mm")
    gauges.tracked_text_center(draw, CX, CY + 84 * SS, config.speed_label().upper(),
                               font(19 * SS, "regular"), TEXT_DIM, tracking=6 * SS)
    rt = "--" if rpm is None else "%d" % int(round(float(rpm)))
    gauges._centered_text(draw, CX, CY + 126 * SS, rt + "  RPM",
                          font(17 * SS, "regular"), mix(BG, TEXT_DIM, 0.7))

    # ---- bottom vitals: coolant + fuel ----------------------------------
    _vital(draw, CX - 96 * SS, W - 78 * SS, "COOLANT",
           _num(snap.coolant_c), config.temp_label(),
           lambda v: int(round(config.c_to_disp(float(v)))),
           _frac(snap.coolant_c, COOLANT_LO, COOLANT_HI),
           warn=lambda v: float(v) > 100, danger=lambda v: float(v) > 110)
    _vital(draw, CX + 96 * SS, W - 78 * SS, "FUEL",
           _num(snap.fuel_pct), "%", lambda v: int(round(float(v))),
           (float(_num(snap.fuel_pct)) / 100.0 if _num(snap.fuel_pct) else 0.0),
           warn=lambda v: float(v) < 20, danger=lambda v: float(v) < 10)

    return img.resize((SAT, SAT), Image.LANCZOS)


def _vital(draw, cx, base_y, label, value, unit, fmt, frac, warn, danger):
    col = TEXT
    if value is not None:
        col = DANGER if danger(value) else (WARN if warn(value) else TEXT)
    gauges.tracked_text_center(draw, cx, base_y - 30 * SS, label,
                               font(13 * SS, "regular"),
                               mix(BG, TEXT_DIM, 0.6), tracking=3 * SS)
    s = "--" if value is None else str(fmt(value))
    draw.text((cx, base_y), s + (" " + unit if value is not None else ""),
              font=font(30 * SS, "thin"), fill=col, anchor="mm")
    bw = 96 * SS
    draw.rounded_rectangle([cx - bw / 2, base_y + 26 * SS, cx + bw / 2,
                            base_y + 26 * SS + 3 * SS], radius=int(1.5 * SS),
                           fill=mix(BG, TEXT_DIM, 0.4))
    fw = int(max(0.0, min(1.0, frac)) * bw)
    if fw > 0:
        draw.rounded_rectangle([cx - bw / 2, base_y + 26 * SS, cx - bw / 2 + fw,
                                base_y + 26 * SS + 3 * SS], radius=int(1.5 * SS),
                               fill=col if col in (WARN, DANGER) else mix(TEXT_DIM, TEXT, 0.3))


def jpeg(snap, quality: int = 72) -> bytes:
    """Render + JPEG-encode the satellite frame (bytes) for streaming."""
    buf = io.BytesIO()
    render(snap).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
