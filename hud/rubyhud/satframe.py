"""480x480 satellite view for the 4" Qualia, rendered ON THE PI in the same
Tesla language as the 7" HUD.

The Pi renders BOTH screens from one Pillow codebase: render(snap) returns a
480x480 RGB image (condensed gauge cluster), jpeg(snap) returns JPEG bytes. The
frame is streamed to the Qualia thin client over USB-serial, so the 4" shares
the 7"'s exact look and every UI change ships via a normal Pi OTA -- the Qualia
firmware (a dumb JPEG-blit client) is flashed once and never touched again.

Square layout: a 270-degree rpm arc (redline segment) wrapping a hero speed
numeral, with the gear above it. Pure near-black, hairlines -- the same
vocabulary as render.py / GaugesPage. Above a selectable rpm threshold the whole
panel blanks to amber (speed + gear only): a peripheral-vision SHIFT LIGHT,
configured from the 7" MENU (config.shift_rpm / shift_enabled).
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from . import config, gauges
from .theme import DANGER, TEXT, TEXT_DIM, font, mix

SAT = 480                      # logical panel size (square)
SS = 2                         # supersample; render at 960 then downsample
W = SAT * SS

RPM_MAX = 8000.0
REDLINE = 7000.0
SHIFT_AMBER = (255, 176, 0)    # full-screen shift-light flash color
# True black background. The shared theme BG is a charcoal (7,9,12); on the 4"
# we render pure (0,0,0) -- the darkest this IPS-LCD panel can show. (It can't
# go truly pitch black: the LED backlight is always on, so black pixels still
# glow faintly. Per-pixel "off" would need an OLED panel.)
BG_SAT = (0, 0, 0)

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


def render(snap) -> Image.Image:
    """Render the 480x480 satellite view for `snap`, applying the configured
    orientation (mirror / rotate) LAST so it reads correctly when the panel is
    reflected off the windshield.

    Once rpm crosses the (selectable) shift threshold the whole panel blanks to
    amber with only the speed + gear left -- see _render_shift."""
    rpm = _num(snap.rpm)
    if (config.shift_enabled() and rpm is not None
            and float(rpm) >= config.shift_rpm()):
        img = _render_shift(snap)
    else:
        img = _render_gauges(snap)
    return _orient(img)


def _orient(img: Image.Image) -> Image.Image:
    """Apply the configured satellite orientation. MIRROR (horizontal flip) is
    the one that matters for a windshield HUD: the glass flips the reflection
    back, so a mirrored frame reads correctly off the windshield. ROTATE 180
    covers an upside-down / inverted mount."""
    if config.sat_rotate():
        img = img.transpose(Image.ROTATE_180)
    if config.sat_mirror():
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return img


def _render_gauges(snap) -> Image.Image:
    """The normal 480x480 gauge cluster: rpm arc + gear + hero speed."""
    rpm = _num(snap.rpm)
    img = Image.new("RGB", (W, W), BG_SAT)
    draw = ImageDraw.Draw(img)
    a1 = ARC_A0 + ARC_SWEEP
    rl0 = ARC_A0 + (REDLINE / RPM_MAX) * ARC_SWEEP

    # ---- rpm arc (track + redline + live) --------------------------------
    gauges.arc_seg(draw, CX, CY, ARC_R, ARC_A0, a1,
                   mix(BG_SAT, TEXT_DIM, 0.30), 3 * SS)
    gauges.arc_seg(draw, CX, CY, ARC_R, rl0, a1, DANGER, 4 * SS)
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
        gauges._centered_text(draw, px, py, lab, tf, mix(BG_SAT, TEXT_DIM, 0.55))

    # ---- gear (top, inside the arc) -------------------------------------
    g = snap.gear or "-"
    draw.text((CX, 150 * SS), g if g != "-" else "–",
              font=font(64 * SS, "thin"), fill=TEXT, anchor="mm")

    # ---- hero speed ------------------------------------------------------
    spd = _num(snap.speed_mph)
    s = "--" if spd is None else "%d" % int(round(config.mph_to_disp(float(spd))))
    draw.text((CX, CY + 6 * SS), s, font=font(150 * SS, "bold"),
              fill=DANGER if in_red else TEXT, anchor="mm")
    gauges.tracked_text_center(draw, CX, CY + 84 * SS, config.speed_label().upper(),
                               font(19 * SS, "regular"), TEXT_DIM, tracking=6 * SS)
    rt = "--" if rpm is None else "%d" % int(round(float(rpm)))
    gauges._centered_text(draw, CX, CY + 126 * SS, rt + "  RPM",
                          font(17 * SS, "regular"), mix(BG_SAT, TEXT_DIM, 0.7))

    return img.resize((SAT, SAT), Image.BOX)   # exact 2x: BOX == supersample resolve


def _render_shift(snap) -> Image.Image:
    """Full-screen amber shift cue. Blank the panel to amber and keep only the
    gear + speed in near-black (max contrast) so the flash is unmistakable at
    the corner of the eye while the two numbers that matter stay legible."""
    img = Image.new("RGB", (W, W), SHIFT_AMBER)
    draw = ImageDraw.Draw(img)
    ink = BG_SAT
    g = snap.gear or "-"
    draw.text((CX, 200 * SS), g if g != "-" else "–",
              font=font(96 * SS, "bold"), fill=ink, anchor="mm")
    spd = _num(snap.speed_mph)
    s = "--" if spd is None else "%d" % int(round(config.mph_to_disp(float(spd))))
    draw.text((CX, CY + 96 * SS), s, font=font(240 * SS, "bold"),
              fill=ink, anchor="mm")
    return img.resize((SAT, SAT), Image.BOX)   # exact 2x: BOX == supersample resolve


def jpeg(snap, quality: int = 72) -> bytes:
    """Render + JPEG-encode the satellite frame (bytes) for streaming."""
    buf = io.BytesIO()
    render(snap).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
