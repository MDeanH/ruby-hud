"""Pure drawing helpers. Each takes an ImageDraw + geometry, clamps
out-of-range/None inputs, and never raises. Sizes are in the caller's
supersampled (SS=2) space; the caller downsamples with LANCZOS.

Expensive raster work (gradient sweeps, blurred glows, meter tracks) is
pre-rendered once into module-level sprite caches; per-frame calls only
paste sprites and draw cheap vector primitives. GaussianBlur is NEVER run
per frame."""

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .theme import (ACCENT, ACCENT_DIM, ACCENT_GLOW, BG, CARD_BORDER,
                    CARD_EDGE, DANGER, NEEDLE, PANEL, TEXT, TEXT_DIM, TICK,
                    WARN, font, mix)

# Sprite cache: key -> Image (RGBA) or tuple of images. Entries are static
# for the life of the process (geometry+palette are fixed), so this never
# needs invalidation.
_SPRITES: dict = {}


# --- small internal utilities ---------------------------------------------
def _clamp01(v):
    try:
        v = float(v)
    except Exception:
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _text_size(draw, text, fnt):
    """Return (w, h) for text, robust across Pillow versions."""
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=fnt)
        return (r - l, b - t)
    except Exception:
        try:
            return draw.textsize(text, font=fnt)
        except Exception:
            return (len(text) * 6, 11)


def _centered_text(draw, cx, cy, text, fnt, fill):
    """Draw text centered (horizontally and vertically) on (cx, cy)."""
    w, h = _text_size(draw, text, fnt)
    try:
        l, t, _, _ = draw.textbbox((0, 0), text, font=fnt)
    except Exception:
        l, t = 0, 0
    draw.text((cx - w / 2 - l, cy - h / 2 - t), text, font=fnt, fill=fill)


def _char_adv(draw, ch, fnt):
    """Advance width of a single character (textlength when available)."""
    try:
        return float(draw.textlength(ch, font=fnt))
    except Exception:
        return float(_text_size(draw, ch, fnt)[0])


def tracked_width(draw, text, fnt, tracking=0.0):
    """Total advance of `text` drawn with manual per-char tracking."""
    txt = str(text)
    if not txt:
        return 0.0
    w = sum(_char_adv(draw, ch, fnt) for ch in txt)
    return w + tracking * (len(txt) - 1)


def tracked_text(draw, x, y, text, fnt, fill, tracking=0.0, anchor=None):
    """Draw text char-by-char with manual advance (letter-spacing).

    `anchor` (e.g. 'ls' for left-baseline) is applied per char when the font
    supports it; falls back to plain top-left drawing. Returns the end x."""
    for ch in str(text):
        try:
            if anchor:
                draw.text((x, y), ch, font=fnt, fill=fill, anchor=anchor)
            else:
                draw.text((x, y), ch, font=fnt, fill=fill)
        except Exception:
            draw.text((x, y), ch, font=fnt, fill=fill)
        x += _char_adv(draw, ch, fnt) + tracking
    return x


def tracked_text_center(draw, cx, cy, text, fnt, fill, tracking=0.0):
    """Letter-spaced text centered (h+v) on (cx, cy)."""
    w = tracked_width(draw, text, fnt, tracking)
    _, h = _text_size(draw, str(text), fnt)
    try:
        _, t, _, _ = draw.textbbox((0, 0), str(text), font=fnt)
    except Exception:
        t = 0
    tracked_text(draw, cx - w / 2, cy - h / 2 - t, text, fnt, fill, tracking)


def kerned_right(draw, right, baseline, text, fnt, fill, tracking=0.0):
    """Right-aligned, baseline-anchored text with manual (tight) tracking.

    Returns the left x of the drawn block."""
    txt = str(text)
    w = tracked_width(draw, txt, fnt, tracking)
    x = right - w
    tracked_text(draw, x, baseline, txt, fnt, fill, tracking, anchor="ls")
    return x


def _polar(cx, cy, r, deg):
    rad = math.radians(deg)
    return (cx + r * math.cos(rad), cy + r * math.sin(rad))


def arc_seg(draw, cx, cy, r, a0, a1, color, width):
    """Thin arc segment on the main canvas. Angles in degrees, 0=3 o'clock,
    increasing clockwise (PIL convention). Cheap; safe for per-frame use."""
    if a1 <= a0:
        return
    draw.arc([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=color,
             width=max(1, int(width)))


def arc_glow(img, cx, cy, r, a0, a1, color, width, blur):
    """Soft-glow arc: a blurred copy of the segment. Runs GaussianBlur, so call
    only from render_static (cached in the static layer), never per frame."""
    if a1 <= a0:
        return
    pad = int(blur * 3 + width + 4)
    size = int(2 * r + 2 * pad)
    if size <= 0:
        return
    spr = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(spr).arc([pad, pad, pad + 2 * r, pad + 2 * r], a0, a1,
                            fill=tuple(color) + (255,), width=max(1, int(width)))
    spr = spr.filter(ImageFilter.GaussianBlur(int(blur)))
    img.paste(spr, (int(cx - r - pad), int(cy - r - pad)), spr)


# Sweep constants for the dial / arc_gauge: 270deg from 135 to 405.
_ARC_START = 135.0
_ARC_END = 405.0
_ARC_SPAN = _ARC_END - _ARC_START  # 270


# --- glow sprites (pre-blurred; cached) -------------------------------------
def glow_dot(radius, color, strength=1.0):
    """Soft round glow sprite (RGBA), blurred ONCE and cached."""
    radius = max(2, int(radius))
    key = ("dot", radius, color, round(float(strength), 2))
    img = _SPRITES.get(key)
    if img is None:
        size = radius * 6
        m = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(m)
        c = size // 2
        d.ellipse([c - radius, c - radius, c + radius, c + radius], fill=255)
        m = m.filter(ImageFilter.GaussianBlur(radius * 0.7))
        # Floor-subtract the blur tail so the sprite box never shows.
        s = _clamp01(strength)
        m = m.point(lambda a: max(0, int((a - 8) * s)))
        img = Image.new("RGBA", (size, size), color + (0,))
        img.putalpha(m)
        _SPRITES[key] = img
    return img


def glow_ring(radius, color, lw, strength=1.0):
    """Soft ring glow sprite (RGBA), blurred ONCE and cached."""
    radius = max(3, int(radius))
    lw = max(1, int(lw))
    key = ("ring", radius, color, lw, round(float(strength), 2))
    img = _SPRITES.get(key)
    if img is None:
        size = (radius + lw * 3) * 2
        m = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(m)
        c = size // 2
        d.ellipse([c - radius, c - radius, c + radius, c + radius],
                  outline=255, width=lw)
        m = m.filter(ImageFilter.GaussianBlur(lw * 0.9))
        s = _clamp01(strength)
        m = m.point(lambda a: max(0, int((a - 6) * s)))
        img = Image.new("RGBA", (size, size), color + (0,))
        img.putalpha(m)
        _SPRITES[key] = img
    return img


def glow_rrect(w, h, radius, color, blur, strength=1.0):
    """Soft rounded-rect glow sprite (RGBA) with `blur` px padding, cached.

    Paste at (x - blur, y - blur) to center it under a w x h card."""
    w = max(4, int(w))
    h = max(4, int(h))
    blur = max(2, int(blur))
    key = ("rrect", w, h, int(radius), color, blur,
           round(float(strength), 2))
    img = _SPRITES.get(key)
    if img is None:
        m = Image.new("L", (w + blur * 2, h + blur * 2), 0)
        d = ImageDraw.Draw(m)
        d.rounded_rectangle([blur, blur, blur + w, blur + h],
                            radius=int(radius), fill=255)
        m = m.filter(ImageFilter.GaussianBlur(blur * 0.6))
        s = _clamp01(strength)
        if s != 1.0:
            m = m.point(lambda a: int(a * s))
        img = Image.new("RGBA", m.size, color + (0,))
        img.putalpha(m)
        _SPRITES[key] = img
    return img


# --- card chrome -------------------------------------------------------------
def card(draw, x0, y0, x1, y1, radius, scale=1, fill=PANEL):
    """Panel card: fill, 1px border, 1px lighter top-edge highlight."""
    radius = int(radius)
    px = max(1, int(scale))
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                           outline=CARD_BORDER, width=px)
    # Top-edge highlight between the corner radii (depth cue).
    draw.line([(x0 + radius, y0 + px), (x1 - radius, y0 + px)],
              fill=CARD_EDGE, width=px)


# --- tach dial (static face + per-frame sweep/needle) ------------------------
_DIAL_FACE = mix(BG, PANEL, 0.55)
_DIAL_TRACK = mix(PANEL, TICK, 0.35)


def dial_static(img, draw, cx, cy, r, redline_frac, rpm_max=8000.0,
                scale=1):
    """Static dial face: plate, track ring, redline sector + glow, minor
    ticks every 250 RPM, major ticks + numerals every 1000, captions."""
    r = max(40, int(r))
    ring_w = max(8, int(r * 0.125))
    rf = _clamp01(redline_frac)

    # Plate (slightly lighter than bg) + rim.
    fr = int(r * 1.045)
    draw.ellipse([cx - fr, cy - fr, cx + fr, cy + fr], fill=_DIAL_FACE,
                 outline=CARD_BORDER, width=max(1, int(scale)))

    # Track ring (270 deg).
    bbox = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(bbox, _ARC_START, _ARC_END, fill=_DIAL_TRACK, width=ring_w)

    # Redline sector + pre-blurred glow (built once into the static layer,
    # so the blur cost is one-time).
    ra0 = _ARC_START + _ARC_SPAN * rf
    pad = ring_w * 2
    size = (r + pad) * 2
    m = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(m)
    mb = [pad, pad, size - pad, size - pad]
    md.arc(mb, ra0, _ARC_END, fill=255, width=ring_w + int(r * 0.03))
    m = m.filter(ImageFilter.GaussianBlur(int(r * 0.045)))
    m = m.point(lambda a: int(a * 0.65))
    glow = Image.new("RGBA", (size, size), DANGER + (0,))
    glow.putalpha(m)
    img.paste(glow, (cx - r - pad, cy - r - pad), glow)
    draw.arc(bbox, ra0, _ARC_END, fill=mix(BG, DANGER, 0.78), width=ring_w)

    # Ticks: minor every 250 RPM, major every 1000.
    n_minor = int(round(rpm_max / 250.0))
    tick_out = r - ring_w - max(2, int(r * 0.008))
    minor_in = tick_out - int(r * 0.045)
    major_in = tick_out - int(r * 0.085)
    for i in range(n_minor + 1):
        f = i / float(n_minor)
        ang = _ARC_START + _ARC_SPAN * f
        major = (i % 4 == 0)
        in_red = f >= rf - 1e-6
        if major:
            col = mix(BG, DANGER, 0.85) if in_red else TEXT_DIM
            p0 = _polar(cx, cy, major_in, ang)
            wdt = max(3, int(r * 0.012))
        else:
            col = mix(BG, DANGER, 0.45) if in_red else TICK
            p0 = _polar(cx, cy, minor_in, ang)
            wdt = max(2, int(r * 0.006))
        p1 = _polar(cx, cy, tick_out, ang)
        draw.line([p0, p1], fill=col, width=wdt)

    # Numerals 0..N (x1000 RPM).
    n_major = int(round(rpm_max / 1000.0))
    nfont = font(int(r * 0.115), "bold")
    num_r = r - ring_w - int(r * 0.175)
    for i in range(n_major + 1):
        f = i / float(n_major)
        ang = _ARC_START + _ARC_SPAN * f
        px, py = _polar(cx, cy, num_r, ang)
        col = mix(TEXT_DIM, DANGER, 0.75) if f >= rf - 1e-6 else TEXT
        _centered_text(draw, px, py, str(i), nfont, col)

    # Captions: small 'x1000 RPM' in the open bottom gap.
    tracked_text_center(draw, cx, cy + int(r * 0.60), "x1000 RPM",
                        font(int(r * 0.055), "bold"),
                        mix(BG, TEXT_DIM, 0.75), tracking=int(r * 0.01))
    # 'RPM' caption (small caps spacing) under the digital readout.
    tracked_text_center(draw, cx, cy + int(r * 0.26), "RPM",
                        font(int(r * 0.075), "bold"), TEXT_DIM,
                        tracking=int(r * 0.028))


def _sweep_sprite(r, ring_w):
    """Pre-rendered 270-deg gradient ring (deep red -> bright tip). Returns
    (rgba_image, alpha_band, R) where R is the sprite center offset."""
    key = ("sweep", int(r), int(ring_w))
    ent = _SPRITES.get(key)
    if ent is None:
        R = int(r) + 4
        size = R * 2
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        inset = max(2, ring_w // 10)
        rr = r - inset
        bbox = [R - rr, R - rr, R + rr, R + rr]
        wdt = ring_w - inset * 2
        step = 1.2
        n = int(_ARC_SPAN / step) + 1
        for i in range(n):
            t = i / float(max(1, n - 1))
            if t < 0.55:
                col = mix(ACCENT_DIM, ACCENT, t / 0.55)
            else:
                col = mix(ACCENT, ACCENT_GLOW, (t - 0.55) / 0.45)
            a0 = _ARC_START + _ARC_SPAN * t
            d.arc(bbox, a0, a0 + step * 2.0, fill=col + (255,), width=wdt)
        ent = (img, img.split()[3], R)
        _SPRITES[key] = ent
    return ent


def dial_sweep(img, cx, cy, r, frac):
    """Per-frame gradient sweep: pieslice alpha mask over the cached ring."""
    frac = _clamp01(frac)
    if frac <= 0.003:
        return
    r = max(40, int(r))
    ring_w = max(8, int(r * 0.125))
    sprite, band, R = _sweep_sprite(r, ring_w)
    size = R * 2
    pie = Image.new("L", (size, size), 0)
    pd = ImageDraw.Draw(pie)
    pd.pieslice([0, 0, size, size], _ARC_START,
                _ARC_START + _ARC_SPAN * frac, fill=255)
    mask = ImageChops.multiply(band, pie)
    img.paste(sprite, (cx - R, cy - R), mask)


def dial_needle(img, draw, cx, cy, r, frac):
    """Per-frame needle: pre-blurred glow sprite under a tapered polygon.
    Rim segment only (r*0.58 .. rim) so the center digits stay clear."""
    frac = _clamp01(frac)
    r = max(40, int(r))
    ring_w = max(8, int(r * 0.125))
    ang = _ARC_START + _ARC_SPAN * frac

    # Glow under the needle tip (mid ring band).
    g = glow_dot(int(ring_w * 0.8), ACCENT_GLOW, strength=0.85)
    gx, gy = _polar(cx, cy, r - ring_w * 0.5, ang)
    img.paste(g, (int(gx) - g.width // 2, int(gy) - g.height // 2), g)

    # Tapered polygon needle.
    tip_r = r - ring_w * 0.12
    tail_r = r * 0.58
    tip = _polar(cx, cy, tip_r, ang)
    tail = _polar(cx, cy, tail_r, ang)
    rad = math.radians(ang + 90.0)
    nx, ny = math.cos(rad), math.sin(rad)
    w_tail = max(2.0, r * 0.016)
    w_tip = max(1.0, r * 0.005)
    poly = [
        (tail[0] + nx * w_tail, tail[1] + ny * w_tail),
        (tail[0] - nx * w_tail, tail[1] - ny * w_tail),
        (tip[0] - nx * w_tip, tip[1] - ny * w_tip),
        (tip[0] + nx * w_tip, tip[1] + ny * w_tip),
    ]
    draw.polygon(poly, fill=NEEDLE)


# --- pill meter (vertical; static track + per-frame gradient fill) ----------
def _pill_track_sprite(w, h, scale):
    """Rounded track with an inner-shadow look. Pre-rendered + cached."""
    key = ("ptrack", int(w), int(h), int(scale))
    img = _SPRITES.get(key)
    if img is None:
        w = int(w)
        h = int(h)
        rad = w // 2
        img = Image.new("RGBA", (w + 1, h + 1), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w, h], radius=rad, fill=(9, 11, 14, 255))
        # Inner shadow: blurred dark inner rim, masked back into the pill.
        sh = Image.new("L", (w + 1, h + 1), 0)
        sd = ImageDraw.Draw(sh)
        sd.rounded_rectangle([0, 0, w, h], radius=rad, outline=255,
                             width=max(2, w // 7))
        sh = sh.filter(ImageFilter.GaussianBlur(max(2, w // 6)))
        sh = sh.point(lambda a: int(a * 0.55))
        pillmask = Image.new("L", (w + 1, h + 1), 0)
        ImageDraw.Draw(pillmask).rounded_rectangle([0, 0, w, h], radius=rad,
                                                   fill=255)
        sh = ImageChops.multiply(sh, pillmask)
        dark = Image.new("RGBA", (w + 1, h + 1), (0, 0, 0, 0))
        dark.putalpha(sh)
        img.paste(dark, (0, 0), dark)
        d.rounded_rectangle([0, 0, w, h], radius=rad, outline=CARD_BORDER,
                            width=max(1, int(scale)))
        _SPRITES[key] = img
    return img


def _pill_fill_sprite(w, h):
    """Full-height vertical gradient fill (bright top -> deep bottom)."""
    key = ("pfill", int(w), int(h))
    img = _SPRITES.get(key)
    if img is None:
        w = int(w)
        h = int(h)
        img = Image.new("RGBA", (w + 1, h + 1), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for yy in range(h + 1):
            t = 1.0 - yy / float(max(1, h))  # 1 at top, 0 at bottom
            if t < 0.55:
                col = mix(ACCENT_DIM, ACCENT, t / 0.55)
            else:
                col = mix(ACCENT, ACCENT_GLOW, (t - 0.55) / 0.45)
            d.line([(0, yy), (w, yy)], fill=col + (255,))
        mask = Image.new("L", (w + 1, h + 1), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius=w // 2,
                                               fill=255)
        img.putalpha(mask)
        _SPRITES[key] = img
    return img


def pill_static(img, draw, x, y, w, h, label, unit, markers=None, scale=1):
    """Static parts of a pill meter: track, 25/50/75% side ticks, threshold
    markers (dim), label above, unit caption below the value slot."""
    x, y, w, h = int(x), int(y), int(w), int(h)
    track = _pill_track_sprite(w, h, scale)
    img.paste(track, (x, y), track)

    # Side ticks at 25/50/75%.
    for f in (0.25, 0.50, 0.75):
        ty = y + h - int(f * h)
        draw.line([(x + w + 3 * scale, ty), (x + w + 9 * scale, ty)],
                  fill=TICK, width=max(1, int(scale)))

    # Threshold markers (dim when inactive; pill_fill brightens them).
    for mk in (markers or []):
        try:
            mf, col = mk[0], mk[1]
        except Exception:
            continue
        my = y + h - int(_clamp01(mf) * h)
        draw.line([(x - 3 * scale, my), (x + w + 3 * scale, my)],
                  fill=mix(BG, col, 0.55), width=max(2, int(scale)))

    if label:
        _centered_text(draw, x + w / 2, y - int(w * 0.55), str(label),
                       font(max(10, int(w * 0.48)), "bold"), TEXT_DIM)
    if unit:
        _centered_text(draw, x + w / 2, y + h + int(w * 1.55), str(unit),
                       font(max(9, int(w * 0.34)), "bold"),
                       mix(BG, TEXT_DIM, 0.8))


def pill_fill(img, draw, x, y, w, h, frac, value_text, markers=None,
              scale=1):
    """Per-frame pill meter: cropped gradient fill, active-threshold glow,
    mono value text below."""
    x, y, w, h = int(x), int(y), int(w), int(h)
    frac = _clamp01(frac)
    inset = max(2, int(scale * 2))
    ih = h - inset * 2
    fh = int(frac * ih)
    if fh > 2:
        sprite = _pill_fill_sprite(w - inset * 2, ih)
        crop = sprite.crop((0, ih - fh, sprite.width, ih + 1))
        img.paste(crop, (x + inset, y + inset + ih - fh), crop)

    state_col = None
    for mk in (markers or []):
        try:
            mf, col, active = mk[0], mk[1], bool(mk[2])
        except Exception:
            continue
        if not active:
            continue
        state_col = col
        my = y + h - int(_clamp01(mf) * h)
        g = glow_dot(int(w * 0.55), col, strength=0.8)
        img.paste(g, (x + w // 2 - g.width // 2, my - g.height // 2), g)
        draw.line([(x - 3 * scale, my), (x + w + 3 * scale, my)],
                  fill=col, width=max(2, int(scale)))

    vt = "--" if value_text is None else str(value_text)
    _centered_text(draw, x + w / 2, y + h + int(w * 0.78), vt,
                   font(max(11, int(w * 0.62)), "mono"),
                   state_col or TEXT)


# --- arc gauge (legacy; kept for back-compat) --------------------------------
def arc_gauge(draw, cx, cy, r, frac, label, value_text,
              redline_frac=None, color=ACCENT):
    """270-deg arc (135->405): TICK track, `color` fill to frac, DANGER
    redline zone (redline_frac..1), major ticks, needle, centered value
    (mono bold) + label. value None -> '--'."""
    frac = _clamp01(frac)
    r = max(8, int(r))
    width = max(4, int(r * 0.16))
    bbox = [cx - r, cy - r, cx + r, cy + r]

    # Background track.
    draw.arc(bbox, _ARC_START, _ARC_END, fill=TICK, width=width)

    # Redline zone on the track.
    if redline_frac is not None:
        rf = _clamp01(redline_frac)
        a0 = _ARC_START + _ARC_SPAN * rf
        draw.arc(bbox, a0, _ARC_END, fill=DANGER, width=width)

    # Filled value arc (drawn over track, but redline shows through where
    # frac doesn't reach).
    if frac > 0.0:
        a1 = _ARC_START + _ARC_SPAN * frac
        # If we are in the redline region, keep red; otherwise accent color.
        draw.arc(bbox, _ARC_START, a1, fill=color, width=width)
        if redline_frac is not None:
            rf = _clamp01(redline_frac)
            if frac > rf:
                ra0 = _ARC_START + _ARC_SPAN * rf
                draw.arc(bbox, ra0, a1, fill=DANGER, width=width)

    # Major tick marks (every 10% of sweep).
    tick_inner = r - width - int(r * 0.06)
    tick_outer = r - width + int(r * 0.01)
    for i in range(11):
        f = i / 10.0
        ang = _ARC_START + _ARC_SPAN * f
        p0 = _polar(cx, cy, tick_inner, ang)
        p1 = _polar(cx, cy, tick_outer, ang)
        draw.line([p0, p1], fill=TICK, width=max(2, int(r * 0.012)))

    # Needle: rim segment only, so the center digits stay clear at any angle.
    nang = _ARC_START + _ARC_SPAN * frac
    tip = _polar(cx, cy, r - width * 0.4, nang)
    tail = _polar(cx, cy, r * 0.58, nang)
    draw.line([tail, tip], fill=NEEDLE, width=max(3, int(r * 0.02)))

    # Center value + label.
    vt = "--" if value_text is None else str(value_text)
    vfont = font(int(r * 0.42), "mono")
    _centered_text(draw, cx, cy - int(r * 0.04), vt, vfont, TEXT)
    if label:
        lfont = font(int(r * 0.16), "bold")
        _centered_text(draw, cx, cy + int(r * 0.34), str(label),
                       lfont, TEXT_DIM)


# --- bar gauge (legacy; kept for back-compat) --------------------------------
def bar_gauge(draw, x, y, w, h, frac, label, value_text, zones=None):
    """Vertical bar. `zones`=[(start_frac,end_frac,color)] warning bands on the
    track; fill height = frac*h from bottom; label above, value below."""
    frac = _clamp01(frac)
    x = int(x)
    y = int(y)
    w = max(6, int(w))
    h = max(10, int(h))
    radius = max(2, w // 4)

    # Track.
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=PANEL)

    # Warning zones on the track.
    if zones:
        for z in zones:
            try:
                s, e, c = z
            except Exception:
                continue
            s = _clamp01(s)
            e = _clamp01(e)
            if e < s:
                s, e = e, s
            zy0 = y + h - int(e * h)
            zy1 = y + h - int(s * h)
            draw.rectangle([x + 1, zy0, x + w - 1, zy1], fill=c)

    # Fill.
    fh = int(frac * h)
    if fh > 0:
        fy0 = y + h - fh
        draw.rounded_rectangle([x, fy0, x + w, y + h], radius=radius,
                               fill=ACCENT)

    # Border.
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                           outline=TICK, width=max(1, w // 20))

    # Label above.
    if label:
        lfont = font(max(10, int(w * 0.55)), "bold")
        _centered_text(draw, x + w / 2, y - int(w * 0.55), str(label),
                       lfont, TEXT_DIM)

    # Value below.
    vt = "--" if value_text is None else str(value_text)
    vfont = font(max(11, int(w * 0.6)), "mono")
    _centered_text(draw, x + w / 2, y + h + int(w * 0.6), vt, vfont, TEXT)


# --- big number ------------------------------------------------------------
def big_number(draw, cx, cy, text, size, color, unit=None):
    """Centered mono-bold number with an optional small unit to the right."""
    txt = "--" if text is None else str(text)
    size = max(8, int(size))
    nfont = font(size, "mono")
    nw, nh = _text_size(draw, txt, nfont)

    ufont = None
    uw = 0
    if unit:
        ufont = font(max(8, int(size * 0.28)), "bold")
        uw, _ = _text_size(draw, str(unit), ufont)
        gap = int(size * 0.10)
    else:
        gap = 0

    total = nw + gap + uw
    left = cx - total / 2

    try:
        nl, nt, _, _ = draw.textbbox((0, 0), txt, font=nfont)
    except Exception:
        nl, nt = 0, 0
    draw.text((left - nl, cy - nh / 2 - nt), txt, font=nfont, fill=color)

    if unit and ufont is not None:
        ux = left + nw + gap
        # Baseline-align the unit near the bottom of the number.
        uy = cy + nh / 2 - _text_size(draw, str(unit), ufont)[1]
        draw.text((ux, uy), str(unit), font=ufont, fill=TEXT_DIM)


# --- gear box --------------------------------------------------------------
def gear_plate(draw, cx, cy, size):
    """Static filled Soul-Red rounded square (gear letter drawn per frame)."""
    size = int(size)
    x0 = cx - size / 2
    y0 = cy - size / 2
    x1 = cx + size / 2
    y1 = cy + size / 2
    rad = int(size * 0.20)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=ACCENT)
    # Subtle brighter top edge for depth.
    draw.line([(x0 + rad, y0 + 1), (x1 - rad, y0 + 1)],
              fill=mix(ACCENT, ACCENT_GLOW, 0.6), width=2)


def gear_value(draw, cx, cy, gear, size):
    """Per-frame gear glyph (white bold) over the static plate."""
    g = "-" if not gear else str(gear)
    _centered_text(draw, cx, cy, g, font(int(size * 0.62), "bold"),
                   (255, 255, 255))


def gear_box(draw, cx, cy, gear, scale=1):
    """One-shot gear indicator (plate + glyph). Kept for back-compat."""
    size = int(130 * scale)
    gear_plate(draw, cx, cy, size)
    gear_value(draw, cx, cy, gear, size)


# --- status chip -----------------------------------------------------------
def status_chip(draw, x, y, text, color, filled=False, scale=1):
    """Rounded pill chip. Returns (width, height) so callers can lay out rows.

    Outline chips get a faint pre-mixed fill (state color at ~18% against BG)
    + a dim color border; filled chips are solid color with dark text.
    `scale` matches the caller's supersample factor."""
    txt = str(text)
    pad_x = int(20 * scale)
    pad_y = int(9 * scale)
    cfont = font(int(25 * scale), "bold")
    tw, th = _text_size(draw, txt, cfont)
    w = tw + pad_x * 2
    h = th + pad_y * 2
    radius = h // 2

    if filled:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=color)
        tcol = (10, 12, 15)
    else:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=mix(BG, color, 0.18),
                               outline=mix(BG, color, 0.45),
                               width=max(1, int(scale)))
        tcol = color

    try:
        l, t, _, _ = draw.textbbox((0, 0), txt, font=cfont)
    except Exception:
        l, t = 0, 0
    draw.text((x + pad_x - l, y + pad_y - t), txt, font=cfont, fill=tcol)
    return (w, h)


# --- warning banner --------------------------------------------------------
def warning_banner(img, draw, x, y, w, texts, t, scale=1):
    """Pulsing DANGER warning card: pre-blurred glow sprite + rounded card
    with a DANGER border; fill/border pulse via pre-mixed colors. No-op when
    `texts` empty; `t` is a 0..1 phase. `scale` matches the supersample."""
    if not texts:
        return
    try:
        phase = float(t)
    except Exception:
        phase = 0.0
    pulse = 0.5 + 0.5 * math.cos(phase * 2 * math.pi)
    if pulse != pulse or not (-1.0 <= pulse <= 2.0):  # NaN/inf guard
        pulse = 0.0

    h = int(66 * scale)
    rad = int(16 * scale)
    blur = int(18 * scale)
    # Quantize glow strength to 0.1 steps so only ~4 pre-blurred sprites are
    # ever built/cached (a continuous value would re-blur every frame).
    glow = glow_rrect(int(w), h, rad, DANGER, blur,
                      strength=round(0.30 + 0.30 * pulse, 1))
    img.paste(glow, (int(x) - blur, int(y) - blur), glow)

    fill = mix(BG, DANGER, 0.16 + 0.14 * pulse)
    border = mix(ACCENT_DIM, DANGER, 0.35 + 0.65 * pulse)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=rad, fill=fill,
                           outline=border, width=max(2, int(2 * scale)))
    msg = "  -  ".join(str(s) for s in texts)
    bfont = font(int(32 * scale), "bold")
    tcol = mix((200, 160, 160), (255, 255, 255), pulse)
    _centered_text(draw, x + w / 2, y + h / 2, msg, bfont, tcol)
