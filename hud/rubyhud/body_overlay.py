"""Tesla-style body / safety overlay for rubyhud.

Drawn over ANY page by render.compose_frame when a monitored body panel is open
(driver / passenger door, trunk -> 0x43E) or a blind-spot vehicle is detected
(0x477). One unified top-down vehicle visualization: a soft-shaded cool-grey
MX-5 on the near-black page, the open panel posed/lit, and a red proximity arc
on the side with a blind-spot vehicle.

Asset-aware: if pre-rendered top-down RF PNG layers are installed under
assets/car/ (car_<L><R><T>.png for the 8 door/trunk combinations, plus
adjacent.png for the blind-spot car), they are composited for a photoreal look.
Until those 3D renders exist, a self-contained vector fallback draws an
equivalent Tesla-style car so the feature works and is verifiable on the bench.

Everything is guarded: a missing asset, a bad value, or any draw error degrades
gracefully (to the vector car, or to nothing) and never raises into the frame
loop. The overlay draws NOTHING when all panels are shut and no blind spot is
active -- zero cost on the common path.
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .theme import font

# Tesla-ish overlay palette (the page already provides the near-black bg).
_RED = (255, 69, 58)        # ff453a  alert / open / proximity
_RED_DEEP = (226, 35, 26)   # e2231a  arc tail
_RED_HOT = (255, 106, 77)   # ff6a4d  arc core
_WHITE = (238, 241, 244)    # eef1f4  primary label / specular
_GREY = (139, 146, 155)     # 8b929b  secondary label
_EDGE = (10, 13, 17)        # body outline / seams

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "car")
_asset_cache: dict = {}     # filename -> RGBA Image | None  (None = absent/bad)

# Ego-car placement (screen px, pre-supersample). Nose points up.
_CX, _CY = 462.0, 430.0
_HL, _HW = 232.0, 76.0      # half-length, max half-width


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _load_asset(name: str) -> Image.Image | None:
    """Load assets/car/<name> as RGBA, cached. Missing/bad -> None (no raise)."""
    if name in _asset_cache:
        return _asset_cache[name]
    img = None
    try:
        path = os.path.join(_ASSET_DIR, name)
        if os.path.isfile(path):
            img = Image.open(path).convert("RGBA")
    except Exception:
        img = None
    _asset_cache[name] = img
    return img


def _stage_lift(img, cx, cy, rx, ry):
    """Soft lighter ellipse to lift the car area off pure black (depth cue)."""
    w, h = int(rx * 2), int(ry * 2)
    if w <= 0 or h <= 0:
        return
    spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(spr).ellipse([0, 0, w, h], fill=(28, 33, 40, 135))
    spr = spr.filter(ImageFilter.GaussianBlur(int(min(rx, ry) * 0.42)))
    img.paste(spr, (int(cx - rx), int(cy - ry)), spr)


def _blurred_shadow(img, cx, cy, rx, ry, blur, alpha):
    """Paste a soft black ambient-occlusion ellipse centered at (cx, cy)."""
    pad = int(blur * 2 + 4)
    w, h = int(rx * 2 + pad * 2), int(ry * 2 + pad * 2)
    if w <= 0 or h <= 0:
        return
    spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(spr)
    d.ellipse([pad, pad, pad + rx * 2, pad + ry * 2], fill=(0, 0, 0, alpha))
    spr = spr.filter(ImageFilter.GaussianBlur(blur))
    img.paste(spr, (int(cx - w / 2), int(cy - h / 2)), spr)


def _silhouette(cx, cy, hl, hw):
    """Top-down roadster silhouette as a closed point list (nose up)."""
    # (t along length, half-width fraction) profile, nose(0) -> tail(1).
    # Longer near-parallel flanks + sharper nose/tail read more car than pod.
    prof = [(0.00, 0.00), (0.03, 0.40), (0.08, 0.66), (0.14, 0.85),
            (0.22, 0.94), (0.32, 0.98), (0.50, 1.00), (0.70, 0.99),
            (0.80, 0.95), (0.88, 0.85), (0.94, 0.62), (0.98, 0.34),
            (1.00, 0.12)]
    top = cy - hl
    left, right = [], []
    for t, f in prof:
        y = top + t * 2 * hl
        left.append((cx - hw * f, y))
        right.append((cx + hw * f, y))
    return left + right[::-1]


def _body_sprite(w, h, vfall_to=0.82):
    """Cool-grey 'painted metal' fill: bright down the centerline (cross-car
    gradient), gently darker toward the tail. Returns an opaque RGBA sprite the
    caller masks to the silhouette."""
    xs = np.linspace(-1.0, 1.0, w, dtype=np.float32)
    b = np.clip(1.0 - np.abs(xs) ** 1.4, 0.0, 1.0)          # 1 center .. 0 edge
    edge = np.array([40, 45, 51], np.float32)
    cen = np.array([210, 216, 222], np.float32)
    col = edge[None, :] * (1 - b[:, None]) + cen[None, :] * b[:, None]  # (w,3)
    arr = np.repeat(col[None, :, :], h, axis=0)             # (h,w,3)
    yfall = np.linspace(1.0, vfall_to, h, dtype=np.float32)[:, None, None]
    arr = np.clip(arr * yfall, 0, 255).astype(np.uint8)
    a = np.full((h, w, 1), 255, np.uint8)
    return Image.fromarray(np.concatenate([arr, a], axis=2), "RGBA")


def _filled_silhouette(pts, vfall_to=0.82):
    """Render a silhouette point list as a gradient-filled RGBA sprite +
    its top-left offset, so the caller can paste it onto the frame."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0 = int(min(xs)), int(min(ys))
    w, h = int(max(xs)) - x0 + 1, int(max(ys)) - y0 + 1
    if w <= 0 or h <= 0:
        return None, 0, 0
    body = _body_sprite(w, h, vfall_to)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for (x, y) in pts], fill=255)
    body.putalpha(mask)
    return body, x0, y0


# --------------------------------------------------------------------------- #
# ego car (vector fallback)
# --------------------------------------------------------------------------- #
def _draw_ego_vector(img, draw, snap, S):
    cx, cy = _CX * S, _CY * S
    hl, hw = _HL * S, _HW * S

    # ground shadow.
    _blurred_shadow(img, cx, cy + 8 * S, hw * 1.9, hl * 1.36,
                    int(22 * S), 150)

    # tyres (under body).
    for tx, ty in ((-hw - 2 * S, -hl * 0.74), (hw + 2 * S, -hl * 0.74),
                   (-hw - 2 * S, hl * 0.70), (hw + 2 * S, hl * 0.70)):
        draw.rounded_rectangle([cx + tx - 11 * S, cy + ty - 32 * S,
                                cx + tx + 11 * S, cy + ty + 32 * S],
                               radius=int(9 * S), fill=(9, 11, 14))

    # body (gradient-filled silhouette).
    pts = _silhouette(cx, cy, hl, hw)
    body, bx, by = _filled_silhouette(pts)
    if body is not None:
        img.paste(body, (bx, by), body)
    draw.line(pts + [pts[0]], fill=_EDGE, width=max(1, int(2 * S)), joint="curve")

    # centerline specular crown (soft).
    crown = Image.new("RGBA", (int(hw * 1.0), int(hl * 1.7)), (0, 0, 0, 0))
    cd = ImageDraw.Draw(crown)
    cd.ellipse([crown.width * 0.30, 0, crown.width * 0.70, crown.height],
               fill=(238, 241, 244, 60))
    crown = crown.filter(ImageFilter.GaussianBlur(int(7 * S)))
    img.paste(crown, (int(cx - crown.width / 2), int(cy - crown.height / 2)),
              crown)

    # windshield + short roadster greenhouse (dark glass).
    gw, gtop, gbot = 58 * S, cy - 0.30 * hl, cy + 0.12 * hl
    draw.rounded_rectangle([cx - gw, gtop, cx + gw, gbot],
                           radius=int(18 * S), fill=(13, 16, 20),
                           outline=(5, 7, 10), width=max(1, int(2 * S)))
    # twin roll hoops.
    for hx in (-26 * S, 26 * S):
        draw.ellipse([cx + hx - 7 * S, cy + 0.18 * hl - 5 * S,
                      cx + hx + 7 * S, cy + 0.18 * hl + 5 * S],
                     fill=(13, 16, 20))

    # side-mirror nibs.
    for mside in (-1, 1):
        mx = cx + mside * (hw + 1 * S)
        draw.polygon([(mx, gtop + 6 * S), (mx + mside * 20 * S, gtop + 12 * S),
                      (mx, gtop + 22 * S)], fill=(120, 128, 138))

    # closed-panel seams (so doors/trunk read as present when shut).
    if not snap.door_right:
        draw.line([(cx + hw * 0.92, cy - 0.16 * hl),
                   (cx + hw * 0.95, cy + 0.22 * hl)], fill=_EDGE,
                  width=max(1, int(2 * S)))
    if not snap.door_left:
        draw.line([(cx - hw * 0.92, cy - 0.16 * hl),
                   (cx - hw * 0.95, cy + 0.22 * hl)], fill=_EDGE,
                  width=max(1, int(2 * S)))

    # trunk: closed seam or lifted panel.
    if snap.trunk:
        _draw_open_trunk(img, draw, cx, cy, hl, hw, S)
    else:
        draw.line([(cx - hw * 0.74, cy + 0.62 * hl),
                   (cx + hw * 0.74, cy + 0.62 * hl)], fill=_EDGE,
                  width=max(1, int(2 * S)))

    # open doors (driver = left, passenger = right; verified on-car).
    if snap.door_left:
        _draw_open_door(img, draw, snap, -1, cx, cy, hl, hw, S)
    if snap.door_right:
        _draw_open_door(img, draw, snap, +1, cx, cy, hl, hw, S)


def _draw_open_door(img, draw, snap, side, cx, cy, hl, hw, S):
    """Swing a door panel outward from the cabin (side=-1 left, +1 right)."""
    hinge_y = cy - 0.10 * hl
    rear_y = cy + 0.22 * hl
    # revealed dark sill aperture on the body.
    draw.polygon([(cx + side * hw * 0.78, hinge_y),
                  (cx + side * hw * 0.98, hinge_y + 4 * S),
                  (cx + side * hw * 0.98, rear_y),
                  (cx + side * hw * 0.78, rear_y)],
                 fill=(8, 10, 14))
    # the swung panel (gradient-filled), hinged at the front.
    panel = [(cx + side * hw * 0.92, hinge_y),
             (cx + side * (hw + 70 * S), hinge_y + 16 * S),
             (cx + side * (hw + 58 * S), rear_y + 8 * S),
             (cx + side * hw * 0.92, rear_y)]
    spr, px, py = _filled_silhouette(panel, vfall_to=0.92)
    if spr is not None:
        img.paste(spr, (px, py), spr)
    # bright leading edge + red accent (open = alert).
    draw.line([panel[0], panel[1]], fill=_WHITE, width=max(1, int(2 * S)))
    draw.line([panel[1], panel[2]], fill=_RED, width=max(1, int(2 * S)))
    _soft_red_edge(img, [panel[1], panel[2]], S)


def _draw_open_trunk(img, draw, cx, cy, hl, hw, S):
    """Lift the rear deck panel up at the tail."""
    y0 = cy + 0.58 * hl
    panel = [(cx - hw * 0.66, y0), (cx + hw * 0.66, y0),
             (cx + hw * 0.50, y0 + 56 * S), (cx - hw * 0.50, y0 + 56 * S)]
    spr, px, py = _filled_silhouette(panel, vfall_to=0.95)
    if spr is not None:
        img.paste(spr, (px, py), spr)
    draw.line([panel[0], panel[1]], fill=_WHITE, width=max(1, int(2 * S)))
    draw.line([panel[2], panel[3]], fill=_RED, width=max(1, int(2 * S)))
    _soft_red_edge(img, [panel[3], panel[2]], S)


def _soft_red_edge(img, seg, S):
    """Soft red glow along a 2-point segment (open-panel alert accent)."""
    (x0, y0), (x1, y1) = seg
    minx, miny = int(min(x0, x1) - 14 * S), int(min(y0, y1) - 14 * S)
    w = int(abs(x1 - x0) + 28 * S)
    h = int(abs(y1 - y0) + 28 * S)
    if w <= 0 or h <= 0:
        return
    spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(spr).line([(x0 - minx, y0 - miny), (x1 - minx, y1 - miny)],
                             fill=(255, 69, 58, 200), width=max(2, int(5 * S)))
    spr = spr.filter(ImageFilter.GaussianBlur(int(5 * S)))
    img.paste(spr, (minx, miny), spr)


# --------------------------------------------------------------------------- #
# blind-spot vehicle + proximity arc
# --------------------------------------------------------------------------- #
def _draw_bsm(img, draw, snap, S):
    """Adjacent vehicle + red proximity arc on each active blind-spot side."""
    for side, active in ((+1, snap.bsm_right), (-1, snap.bsm_left)):
        if not active:
            continue
        acx = _CX * S + side * 380 * S
        acy = _CY * S - 18 * S
        ahl, ahw = 196 * S, 60 * S

        _blurred_shadow(img, acx, acy + 8 * S, ahw * 1.5, ahl * 1.12,
                        int(20 * S), 120)

        # adjacent car: real asset if present, else a dimmer vector body.
        adj = _load_asset("adjacent.png")
        if adj is not None:
            scaled = _scale_to_h(adj, int(ahl * 2.1))
            if side < 0:
                scaled = scaled.transpose(Image.FLIP_LEFT_RIGHT)
            img.paste(scaled, (int(acx - scaled.width / 2),
                               int(acy - scaled.height / 2)), scaled)
        else:
            pts = _silhouette(acx, acy, ahl, ahw)
            spr, bx, by = _filled_silhouette(pts, vfall_to=0.7)
            if spr is not None:
                img.paste(spr, (bx, by), spr)
            draw.line(pts + [pts[0]], fill=_EDGE, width=max(1, int(2 * S)),
                      joint="curve")
            draw.rounded_rectangle([acx - ahw * 0.6, acy - 0.18 * ahl,
                                    acx + ahw * 0.6, acy + 0.26 * ahl],
                                   radius=int(14 * S), fill=(13, 16, 20))

        # layered proximity arc hugging the near (toward-ego) side.
        near = acx - side * (ahw + 16 * S)
        bow = side * 26 * S
        arc = [(near, acy - 0.52 * ahl),
               (near - bow, acy),
               (near, acy + 0.52 * ahl)]
        _glow_curve(img, arc, _RED_DEEP, int(7 * S), 200, int(5 * S))
        _glow_curve(img, arc, _RED, int(4 * S), 235, int(4 * S))
        draw.line(arc, fill=_RED_HOT, width=max(2, int(3 * S)), joint="curve")

        # 'do not merge' bar between ego and the adjacent car.
        bar_x = _CX * S + side * 244 * S
        draw.rounded_rectangle([bar_x - 3 * S, acy - 0.34 * ahl,
                                bar_x + 3 * S, acy + 0.34 * ahl],
                               radius=int(3 * S), fill=_RED)


def _glow_curve(img, pts, color, width, alpha, blur):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = blur * 2 + width
    x0, y0 = int(min(xs) - pad), int(min(ys) - pad)
    w = int(max(xs) - min(xs) + pad * 2)
    h = int(max(ys) - min(ys) + pad * 2)
    if w <= 0 or h <= 0:
        return
    spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(spr).line([(x - x0, y - y0) for (x, y) in pts],
                             fill=color + (alpha,), width=width, joint="curve")
    spr = spr.filter(ImageFilter.GaussianBlur(blur))
    img.paste(spr, (x0, y0), spr)


def _scale_to_h(im, h):
    if im.height <= 0:
        return im
    w = max(1, int(im.width * h / im.height))
    return im.resize((w, max(1, int(h))), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# captions
# --------------------------------------------------------------------------- #
def _caption(draw, x, y, title, sub, dot, S, anchor="lm"):
    tf = font(int(26 * S), "regular")
    sf = font(int(16 * S), "regular")
    if anchor == "rm":
        draw.ellipse([x - 4 * S, y - 4 * S, x + 4 * S, y + 4 * S], fill=dot)
        draw.text((x - 16 * S, y), title, font=tf, fill=_WHITE, anchor="rm")
        if sub:
            draw.text((x - 16 * S, y + 26 * S), sub, font=sf, fill=_GREY,
                      anchor="rm")
    else:
        draw.ellipse([x - 4 * S, y - 4 * S, x + 4 * S, y + 4 * S], fill=dot)
        draw.text((x + 16 * S, y), title, font=tf, fill=_WHITE, anchor="lm")
        if sub:
            draw.text((x + 16 * S, y + 26 * S), sub, font=sf, fill=_GREY,
                      anchor="lm")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def draw_overlay(img, draw, snap) -> None:
    """Render the body/safety overlay onto `img` (RGB, supersampled). No-op when
    nothing is open and no blind spot is active. Never raises."""
    try:
        panel_open = bool(snap.door_left or snap.door_right or snap.trunk)
        bsm = bool(snap.bsm_left or snap.bsm_right)
        if not (panel_open or bsm):
            return

        S = max(1, img.width // 1280)

        # Dim the underlying page so the visualization is the focus (Tesla-style
        # takeover), with a soft stage lift behind the ego car for depth.
        scrim = Image.new("RGBA", img.size, (6, 7, 9, 190))
        img.paste(scrim, (0, 0), scrim)
        _stage_lift(img, int(_CX * S), int((_CY + 4) * S),
                    int(540 * S), int(350 * S))

        # ego car: photoreal asset combo if installed, else vector fallback.
        combo = "car_%d%d%d.png" % (int(bool(snap.door_left)),
                                    int(bool(snap.door_right)),
                                    int(bool(snap.trunk)))
        asset = _load_asset(combo) or _load_asset("car_000.png")
        if asset is not None:
            _blurred_shadow(img, _CX * S, (_CY + 8) * S, _HW * 1.9 * S,
                            _HL * 1.36 * S, int(22 * S), 150)
            scaled = _scale_to_h(asset, int(_HL * 2.18 * S))
            img.paste(scaled, (int(_CX * S - scaled.width / 2),
                               int(_CY * S - scaled.height / 2)), scaled)
        else:
            _draw_ego_vector(img, draw, snap, S)

        if bsm:
            _draw_bsm(img, draw, snap, S)

        # captions (left = body status, right = blind spot).
        if snap.door_left:
            _caption(draw, 250 * S, 612 * S, "Driver door open",
                     "Front left · ajar", _RED, S, anchor="lm")
        elif snap.door_right:
            _caption(draw, 250 * S, 612 * S, "Passenger door open",
                     "Front right · ajar", _RED, S, anchor="lm")
        elif snap.trunk:
            _caption(draw, 250 * S, 612 * S, "Trunk open",
                     "Boot · ajar", _RED, S, anchor="lm")
        # secondary line if multiple body panels open.
        extras = []
        if snap.door_left and snap.door_right:
            extras.append("Passenger door")
        if snap.trunk and (snap.door_left or snap.door_right):
            extras.append("Trunk")
        if extras:
            draw.text((266 * S, 654 * S), "Also open: " + ", ".join(extras),
                      font=font(int(16 * S), "regular"), fill=_GREY, anchor="lm")

        if snap.bsm_right:
            _caption(draw, 1252 * S, 372 * S, "Vehicle in blind spot",
                     "Right side · do not merge", _RED, S, anchor="rm")
        if snap.bsm_left:
            _caption(draw, 28 * S, 372 * S, "Vehicle in blind spot",
                     "Left side · do not merge", _RED, S, anchor="lm")
    except Exception:
        pass
