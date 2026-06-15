"""Top-down line-art MX-5 for the BODY page (Tesla design language).

A clean, minimal top-down ND RF roadster drawn with hairline strokes on the
near-black field: a light-grey body outline with a faint fill, thin interior
seams (hood / windshield / hardtop / decklid / door cuts), four tyres. Open
panels (driver/passenger door, trunk) swing/lift out in red with a soft glow;
a blind-spot vehicle shows a red arc on the active side. Nose points up.

No assets, no gradients, no 3D — pure vector in the same hairline vocabulary as
the gauges. (A photoreal Blender render is the planned later upgrade; see
tools/blender_render_rf.py.) Everything is guarded; never raises into the loop.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFilter

from .theme import BG, DANGER, TEXT, mix

# hairline palette
_OUTLINE = mix(BG, TEXT, 0.74)     # body edge — calm light grey
_DETAIL = mix(BG, TEXT, 0.34)      # interior seams — dim
_FILL = mix(BG, TEXT, 0.05)        # barely-there body fill (presence, not weight)
_GLASS = mix(BG, TEXT, 0.10)       # hardtop / glass panel
_TYRE = (9, 11, 14)
_RED = DANGER

# Top-down ND RF half-width profile, nose(0) -> tail(1) as a fraction of HW.
# Rounded nose + tail, widest across the rear haunches (sporty stance).
_PROF = [
    (0.00, 0.28), (0.05, 0.54), (0.11, 0.76), (0.18, 0.87), (0.27, 0.92),
    (0.38, 0.95), (0.50, 0.97), (0.60, 1.00), (0.69, 0.985), (0.78, 0.90),
    (0.86, 0.74), (0.93, 0.52), (1.00, 0.30),
]


def _prof_at(t):
    t = max(0.0, min(1.0, t))
    pts = _PROF
    for i in range(len(pts) - 1):
        t0, f0 = pts[i]
        t1, f1 = pts[i + 1]
        if t <= t1:
            k = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return f0 + (f1 - f0) * k
    return pts[-1][1]


def _silhouette(cx, cy, hl, hw):
    top = cy - hl
    left, right = [], []
    n = 64
    for i in range(n + 1):
        t = i / n
        f = _prof_at(t)
        y = top + t * 2 * hl
        left.append((cx - hw * f, y))
        right.append((cx + hw * f, y))
    return left + right[::-1]


def _edge(cx, cy, hl, hw, t, side):
    return (cx + side * hw * _prof_at(t), (cy - hl) + t * 2 * hl)


def _soft_line(img, pts, color, width, blur, alpha):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = int(blur * 3 + width + 4)
    x0, y0 = int(min(xs) - pad), int(min(ys) - pad)
    w = int(max(xs) - min(xs) + pad * 2)
    h = int(max(ys) - min(ys) + pad * 2)
    if w <= 0 or h <= 0:
        return
    spr = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(spr).line([(x - x0, y - y0) for (x, y) in pts],
                             fill=tuple(color) + (alpha,), width=max(1, int(width)),
                             joint="curve")
    spr = spr.filter(ImageFilter.GaussianBlur(int(blur)))
    img.paste(spr, (x0, y0), spr)


def _tyre(draw, cx, cy, w, h, S):
    draw.rounded_rectangle([cx - w, cy - h, cx + w, cy + h],
                           radius=int(min(w, h) * 0.6), fill=_TYRE,
                           outline=_DETAIL, width=max(1, int(1.5 * S)))


def _door(img, draw, snap, side, cx, cy, hl, hw, S, open_):
    t0, t1 = 0.40, 0.605                     # door cut along the length
    inset = 0.86                             # seam sits just inboard of the edge
    p0 = (cx + side * hw * _prof_at(t0) * inset, (cy - hl) + t0 * 2 * hl)
    p1 = (cx + side * hw * _prof_at(t1) * inset, (cy - hl) + t1 * 2 * hl)
    if not open_:
        draw.line([p0, p1], fill=_DETAIL, width=max(1, int(1.6 * S)))
        return
    # OPEN: dark sill aperture on the body, then the swung panel in red.
    e0 = _edge(cx, cy, hl, hw, t0, side)
    e1 = _edge(cx, cy, hl, hw, t1, side)
    draw.line([e0, e1], fill=(6, 8, 11), width=max(2, int(5 * S)))
    # hinged at the front: the rear of the door swings out (Tesla-style).
    panel = [e0,
             (e0[0] + side * 16 * S, e0[1] + 2 * S),
             (e1[0] + side * 74 * S, e1[1] + 4 * S),
             e1]
    draw.polygon(panel, fill=_FILL)
    draw.line(panel + [panel[0]], fill=_RED, width=max(2, int(2.5 * S)),
              joint="curve")
    _soft_line(img, [panel[1], panel[2]], _RED, 5 * S, 5 * S, 200)


def _trunk(img, draw, cx, cy, hl, hw, S, open_):
    ty = (cy - hl) + 0.80 * 2 * hl           # decklid seam
    w = hw * _prof_at(0.80) * 0.84
    if not open_:
        draw.line([(cx - w, ty), (cx + w, ty)], fill=_DETAIL,
                  width=max(1, int(1.6 * S)))
        return
    # OPEN: dark gap at the seam, then the lifted decklid in red below the tail.
    draw.line([(cx - w, ty), (cx + w, ty)], fill=(6, 8, 11), width=max(2, int(5 * S)))
    lift = [(cx - w, ty + 6 * S), (cx + w, ty + 6 * S),
            (cx + w * 0.82, ty + 64 * S), (cx - w * 0.82, ty + 64 * S)]
    draw.polygon(lift, fill=_FILL)
    draw.line(lift + [lift[0]], fill=_RED, width=max(2, int(2.5 * S)),
              joint="curve")
    _soft_line(img, [lift[3], lift[2]], _RED, 5 * S, 5 * S, 200)


def _bsm(img, draw, side, cx, cy, hl, hw, S):
    ax = cx + side * (hw + 78 * S)
    bow = side * 30 * S
    arc = [(ax - bow, cy - 0.34 * hl), (ax, cy), (ax - bow, cy + 0.34 * hl)]
    _soft_line(img, arc, _RED, 7 * S, 6 * S, 170)
    draw.line(arc, fill=_RED, width=max(2, int(3 * S)), joint="curve")


def draw_car(img, draw, snap, S, *, cx=None, cy=None, car_len=560):
    """Render the top-down MX-5 with live door/trunk/BSM state. car_len is the
    nose-to-tail length in screen px (× S internally)."""
    try:
        if cx is None:
            cx = img.width // 2
        if cy is None:
            cy = img.height // 2
        hl = car_len * S / 2.0
        hw = hl * 0.44

        # soft contact shadow under the car (depth without a heavy fill).
        _soft_line(img, [(cx, cy - hl * 0.7), (cx, cy + hl * 0.7)],
                   (0, 0, 0), hw * 1.6, int(26 * S), 150)

        # body: faint fill + crisp hairline outline.
        pts = _silhouette(cx, cy, hl, hw)
        draw.polygon(pts, fill=_FILL)
        draw.line(pts + [pts[0]], fill=_OUTLINE, width=max(2, int(2.4 * S)),
                  joint="curve")

        # tyres at the front + rear axles, just inside the body edge.
        for t in (0.20, 0.74):
            ex = hw * _prof_at(t)
            y = (cy - hl) + t * 2 * hl
            for side in (-1, 1):
                _tyre(draw, cx + side * (ex - 5 * S), y, 7 * S, 26 * S, S)

        # interior seams (hood cowl, windshield base, decklid line).
        for t, fr in ((0.30, 0.66), (0.355, 0.60), (0.64, 0.70)):
            w = hw * _prof_at(t) * fr
            yy = (cy - hl) + t * 2 * hl
            draw.line([(cx - w, yy), (cx + w, yy)], fill=_DETAIL,
                      width=max(1, int(1.6 * S)))

        # RF hardtop panel (a distinct rounded rect = the retractable hardtop).
        rt0, rt1 = 0.40, 0.615
        rw = hw * 0.60
        draw.rounded_rectangle(
            [cx - rw, (cy - hl) + rt0 * 2 * hl,
             cx + rw, (cy - hl) + rt1 * 2 * hl],
            radius=int(20 * S), fill=_GLASS, outline=_DETAIL,
            width=max(1, int(1.6 * S)))

        # side mirrors (small nibs just outside the body at the cowl).
        for side in (-1, 1):
            mx, my = _edge(cx, cy, hl, hw, 0.375, side)
            draw.polygon([(mx, my - 4 * S), (mx + side * 18 * S, my + 2 * S),
                          (mx, my + 12 * S)], fill=_DETAIL)

        # doors (driver = left, passenger = right) + trunk.
        _door(img, draw, snap, -1, cx, cy, hl, hw, S, bool(snap.door_left))
        _door(img, draw, snap, +1, cx, cy, hl, hw, S, bool(snap.door_right))
        _trunk(img, draw, cx, cy, hl, hw, S, bool(snap.trunk))

        # blind-spot arcs.
        if snap.bsm_left:
            _bsm(img, draw, -1, cx, cy, hl, hw, S)
        if snap.bsm_right:
            _bsm(img, draw, +1, cx, cy, hl, hw, S)
    except Exception:
        pass
