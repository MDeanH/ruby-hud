"""Frame composition for rubyhud.

compose_frame(snap, w, h, ui) renders the full HUD: global top/bottom strips,
the active page body (ui['pages'][ui['page_idx']].render), then nav chrome
(page dots + name, edge chevrons, tap feedback). Everything is drawn on a 2x
supersampled canvas (2560x1600) and downsampled with LANCZOS for clean
anti-aliasing.

StaticLayer cache: all truly static content (background luminance ramp +
vignette, top/bottom strip chrome, per-page card chrome / dial face / nav
dots) is rendered ONCE per (page_name, w, h) into an RGB image at supersample
resolution. Per-frame work is: copy cached background + draw only dynamic
elements. Nothing invalidates the cache (static is truly static)."""

from __future__ import annotations

import os
import time

import numpy as np
from PIL import Image, ImageDraw

from . import gauges
from .theme import (ACCENT, ACCENT_GLOW, BG, BG_TOP, CARD_BORDER, DANGER, OK,
                    PANEL, TEXT, TEXT_DIM, TICK, WARN, font, mix)

W, H = 1280, 800
SS = 2
SW, SH = W * SS, H * SS  # supersampled dimensions

# Gauge / signal ranges (shared with pages.GaugesPage).
RPM_MAX = 8000.0
RPM_REDLINE = 7000.0
COOLANT_LO, COOLANT_HI = 40.0, 120.0
VOLTS_LO, VOLTS_HI = 10.0, 15.0

# Tap-feedback animation (screen px / seconds).
TAP_FX_S = 0.3
TAP_FX_R0, TAP_FX_R1 = 20.0, 48.0

# Static layer caches (never invalidated; see module docstring).
_BASE_CACHE: dict = {}    # (w, h) -> RGB Image: bg ramp + global chrome
_STATIC_CACHE: dict = {}  # (page_name, w, h) -> RGB Image: base + page static


def _num(value):
    """Return value unless it is None/NaN/inf, in which case None.

    Used to gate value-text formatting so a bad CAN decode renders '--'
    instead of crashing compose_frame (int(round(NaN))/(inf), %.1f%inf)."""
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return value


def _frac(value, lo, hi):
    if value is None:
        return 0.0
    try:
        v = float(value)
    except Exception:
        return 0.0
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


# --------------------------------------------------------------------------- #
# static layer: background + global chrome
# --------------------------------------------------------------------------- #
def _gradient_bg(sw: int, sh: int) -> Image.Image:
    """Vertical luminance ramp (BG_TOP -> BG) + faint corner vignette,
    lightly dithered so the 16-bit framebuffer shows no banding."""
    yy = np.linspace(0.0, 1.0, sh, dtype=np.float32)[:, None]
    top = np.asarray(BG_TOP, dtype=np.float32)
    bot = np.asarray(BG, dtype=np.float32)
    ramp = top[None, None, :] * (1.0 - yy[:, :, None]) \
        + bot[None, None, :] * yy[:, :, None]          # (sh, 1, 3)

    xs = np.linspace(-1.0, 1.0, sw, dtype=np.float32)[None, :]
    ys = np.linspace(-1.0, 1.0, sh, dtype=np.float32)[:, None]
    d = np.sqrt(xs * xs + ys * ys) / np.sqrt(2.0)      # 0 center, 1 corner
    vig = np.clip((d - 0.55) / 0.45, 0.0, 1.0)
    factor = (1.0 - 0.22 * vig * vig)[:, :, None]      # (sh, sw, 1)

    arr = ramp * factor
    rng = np.random.default_rng(7)
    arr += rng.uniform(-0.6, 0.6, size=(sh, sw, 1)).astype(np.float32)
    arr = np.clip(arr + 0.5, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


TOP_STRIP_H = 70   # screen px
BOT_SEP_Y = 736    # screen px


def _chrome_static(w: int, h: int) -> Image.Image:
    """Background + static parts of the global top/bottom strips."""
    key = (w, h)
    cached = _BASE_CACHE.get(key)
    if cached is not None:
        return cached

    sw, sh = w * SS, h * SS
    img = _gradient_bg(sw, sh)
    draw = ImageDraw.Draw(img)

    # Top + bottom hairline separators only — no recessed band, no accent bar.
    # A clean near-black field top to bottom is the Tesla language.
    strip_h = TOP_STRIP_H * SS
    hair = mix(BG, CARD_BORDER, 0.6)
    draw.line([(0, strip_h), (sw, strip_h)], fill=hair, width=SS)
    draw.line([(0, BOT_SEP_Y * SS), (sw, BOT_SEP_Y * SS)], fill=hair, width=SS)

    # Wordmark (letter-spaced, calm). The live-status dot + channel are drawn
    # dynamically just to its right by _draw_top_bar.
    gauges.tracked_text(draw, 40 * SS, 22 * SS, "RUBY", font(29 * SS, "bold"),
                        TEXT, tracking=7 * SS)

    _BASE_CACHE[key] = img
    return img


def _draw_nav_static(draw, img, pages, idx):
    """Page dots + name (static per page) and subtle edge chevrons. Dots track
    only the VISIBLE swipe rotation; a hidden page (CAN / PLAYBACK, reached via
    a CONFIGURE deep-link) shows a back hint instead of dots/chevrons."""
    sw = img.width
    cur = pages[idx] if 0 <= idx < len(pages) else None
    if cur is None:
        return

    if getattr(cur, "hidden", False):
        gauges.tracked_text_center(draw, sw // 2, 770 * SS,
                                   "<  HOLD OR SWIPE TO GO BACK",
                                   font(16 * SS, "bold"), TEXT_DIM,
                                   tracking=3 * SS)
        return

    visible = [p for p in pages if not getattr(p, "hidden", False)]
    n = len(visible)
    if n == 0:
        return
    try:
        vidx = visible.index(cur)
    except ValueError:
        vidx = 0
    r = 8 * SS
    gap = 34 * SS
    cy = 757 * SS
    cx0 = sw // 2 - ((n - 1) * gap) // 2
    for i in range(n):
        cx = cx0 + i * gap
        box = [cx - r, cy - r, cx + r, cy + r]
        if i == vidx:
            g = gauges.glow_dot(int(r * 1.7), ACCENT_GLOW, strength=0.8)
            img.paste(g, (cx - g.width // 2, cy - g.height // 2), g)
            draw.ellipse(box, fill=ACCENT)
        else:
            draw.ellipse(box, fill=mix(BG, TEXT_DIM, 0.35))
    gauges.tracked_text_center(draw, sw // 2, 783 * SS,
                               str(getattr(cur, "name", "")),
                               font(16 * SS, "bold"), TEXT_DIM, tracking=4 * SS)

    # Invisible edge tap zones, hinted only by small mid-height chevrons.
    ch = font(40 * SS, "bold")
    gauges._centered_text(draw, 16 * SS, 400 * SS, "<", ch,
                          mix(BG, TEXT_DIM, 0.6))
    gauges._centered_text(draw, sw - 16 * SS, 400 * SS, ">", ch,
                          mix(BG, TEXT_DIM, 0.6))


def _page_static(pages, idx, w: int, h: int) -> Image.Image:
    """Cached static layer for a page: global chrome + page chrome + nav."""
    page = pages[idx]
    # Units are baked into static labels (gauge pill / tile unit text), so the
    # active unit is part of the cache identity -- toggling C/F or MPH/KM-h
    # changes the key and forces a one-time re-render of the static layer.
    from . import config
    key = (str(getattr(page, "name", "PAGE")), w, h,
           config.temp_unit(), config.speed_unit())
    cached = _STATIC_CACHE.get(key)
    if cached is not None:
        return cached

    img = _chrome_static(w, h).copy()
    draw = ImageDraw.Draw(img)
    try:
        page.render_static(draw, img)
    except Exception:
        pass
    _draw_nav_static(draw, img, pages, idx)
    _STATIC_CACHE[key] = img
    return img


# --------------------------------------------------------------------------- #
# dynamic global chrome
# --------------------------------------------------------------------------- #
def _dot(draw, cx, cy, r, col):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)


def _draw_top_bar(draw, snap):
    """Clean Tesla top bar: a calm live-status dot beside the wordmark, a
    centered clock, CPU temp at the right. No 'LIVE' chip, no pills — when live
    the dot alone says so; SIM / NO DATA surface in colour."""
    from . import config
    cy = 36 * SS
    # left: status dot beside RUBY. Live = quiet green dot (no word). SIM / NO
    # DATA add a short coloured label.
    src = snap.source or "NO DATA"
    if src == "LIVE":
        _dot(draw, 182 * SS, cy, 5 * SS, OK)
    else:
        col = WARN if src == "SIM" else DANGER
        _dot(draw, 182 * SS, cy, 5 * SS, col)
        gauges.tracked_text(draw, 198 * SS, cy, src, font(18 * SS, "regular"),
                            col, tracking=2 * SS, anchor="lm")

    # center: clock.
    draw.text((SW // 2, cy), snap.clock or "--:--", font=font(28 * SS, "mono"),
              fill=TEXT, anchor="mm")

    # right: CPU temp (thin grey).
    cpu = snap.cpu_temp_c
    cpu_txt = ("CPU --" if cpu is None
               else "CPU %d%s" % (int(round(config.c_to_disp(cpu))),
                                  config.temp_label()))
    draw.text((SW - 40 * SS, cy), cpu_txt, font=font(20 * SS, "regular"),
              fill=TEXT_DIM, anchor="rm")


def _draw_bottom_strip(draw, snap):
    """Bottom status as thin tracked text (no pills): bus dot + fps and any
    LISTEN-ONLY flag at the left, fuel at the right. The page-nav dots own the
    centre (drawn statically)."""
    cy = 757 * SS
    # left: bus state.
    state = snap.can_bus_state or "NO BUS"
    bus_ok = state == "UP" and (snap.can_fps or 0) > 0
    if bus_ok:
        _dot(draw, 46 * SS, cy, 5 * SS, OK)
        x = gauges.tracked_text(draw, 62 * SS, cy,
                                "CAN  ·  %d fps" % int(snap.can_fps or 0),
                                font(17 * SS, "regular"), TEXT_DIM,
                                tracking=2 * SS, anchor="lm")
    else:
        col = DANGER if state == "ERROR" else TEXT_DIM
        _dot(draw, 46 * SS, cy, 5 * SS, col)
        x = gauges.tracked_text(draw, 62 * SS, cy, "BUS " + state,
                                font(17 * SS, "regular"), col,
                                tracking=2 * SS, anchor="lm")
    if snap.can_listen_only:
        gauges.tracked_text(draw, x + 24 * SS, cy, "LISTEN-ONLY",
                            font(15 * SS, "bold"), WARN, tracking=2 * SS,
                            anchor="lm")

    # right: fuel.
    if _num(snap.fuel_pct) is not None:
        fuel = max(0.0, min(100.0, float(snap.fuel_pct)))
        fcol = DANGER if fuel < 12 else (WARN if fuel < 25 else TEXT_DIM)
        draw.text((SW - 40 * SS, cy), "FUEL %d%%" % int(round(fuel)),
                  font=font(18 * SS, "regular"), fill=fcol, anchor="rm")


def _draw_tap_fx(img, fx):
    """Soft expanding glow ring for 0.3s after a tap. fx=(x, y, t0).

    Uses a pre-blurred ring sprite scaled by phase with a fading alpha; the
    blur itself is one-time (sprite cache)."""
    if not fx:
        return
    try:
        x, y, t0 = fx
        age = time.time() - float(t0)
    except Exception:
        return
    if age < 0.0 or age >= TAP_FX_S:
        return
    p = age / TAP_FX_S
    r = int((TAP_FX_R0 + (TAP_FX_R1 - TAP_FX_R0) * p) * SS)
    fade = 1.0 - p
    base = gauges.glow_ring(40 * SS, ACCENT_GLOW, 6 * SS)
    size = max(8, int(base.width * (r / float(40 * SS))))
    sprite = base.resize((size, size))
    alpha = sprite.split()[3].point(lambda a: int(a * fade))
    cx, cy = int(float(x) * SS), int(float(y) * SS)
    img.paste(sprite, (cx - size // 2, cy - size // 2), alpha)


# --------------------------------------------------------------------------- #
# compose
# --------------------------------------------------------------------------- #
def compose_frame(snap, w: int = W, h: int = H, ui: dict | None = None
                  ) -> Image.Image:
    """Render `snap` + ui state into an RGB (w x h) PIL Image."""
    if ui is None:
        # Bare call (back-compat): default to page 0 with a fresh ctx.
        from . import pages as _pages  # function-level: no import cycle
        ui = {"page_idx": 0, "pages": _pages.make_pages(), "tap_fx": None,
              "ctx": _pages.make_ctx(os.environ.get("RUBYHUD_CHANNEL",
                                                    "can0"))}

    pages = ui.get("pages") or []
    if pages:
        idx = int(ui.get("page_idx", 0)) % len(pages)
        img = _page_static(pages, idx, w, h).copy()
    else:
        idx = None
        img = _chrome_static(w, h).copy()
    draw = ImageDraw.Draw(img)

    _draw_top_bar(draw, snap)
    _draw_bottom_strip(draw, snap)

    if idx is not None:
        pages[idx].render(draw, img, snap, ui.get("ctx", {}))

    _draw_tap_fx(img, ui.get("tap_fx"))

    return img.resize((w, h), Image.LANCZOS)
