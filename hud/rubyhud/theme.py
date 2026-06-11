"""Theme constants and font loading for rubyhud.

Palette colors are RGB tuples ("premium cluster" palette: deep charcoal with
Soul Red accents). Old constant names are kept as aliases of the new palette
so existing callers keep working. Fonts are loaded from DejaVu TTFs with safe
fallbacks (macOS build-host faces, then PIL's default) and never raise.
"""

from PIL import ImageFont

# --- Palette (RGB) ---------------------------------------------------------
# Tokens bound to Michael's "Mazda HUD" mockup (Downloads/Mazda HUD
# (standalone).html): bg #07090c, ring track #2a3340, bright track #4a5666,
# accent #d0273b, dim text #8d99a7.
# Background ramp: slightly lighter at the top of the screen, deep at bottom.
BG = (7, 9, 12)              # 07090C deep charcoal (bottom of ramp)
BG_TOP = (12, 16, 22)        # 0C1016 top of the luminance ramp

# Cards / panels.
PANEL = (16, 20, 27)         # 10141B card fill
CARD = PANEL                 # alias
CARD_BORDER = (42, 51, 64)   # 2A3340 1px card border (mockup ring track)
CARD_EDGE = (58, 68, 84)     # 3A4454 1px lighter top-edge highlight

# Accent + state colors.
ACCENT = (208, 39, 59)       # D0273B mockup Soul Red
ACCENT_GLOW = (255, 77, 92)  # FF4D5C brighter glow tone
ACCENT_DIM = (104, 20, 30)   # deep red (gradient tails, dim accents)
DANGER = (255, 59, 48)       # FF3B30
WARN = (255, 179, 0)         # FFB300
OK = (46, 204, 113)          # 2ECC71

# Text / strokes.
TEXT = (243, 247, 251)       # F3F7FB (mockup bright text)
TEXT_DIM = (141, 153, 167)   # 8D99A7 (mockup dim text)
TICK = (42, 51, 64)          # 2A3340 gauge tracks / tick marks (mockup)
TICK_BRIGHT = (74, 86, 102)  # 4A5666 brighter track (mockup)
NEEDLE = (245, 247, 250)     # needle color

# Table zebra rows (CAN page).
ROW_A = (15, 19, 25)         # 0F1319
ROW_B = (11, 14, 19)         # 0B0E13


def mix(a, b, t):
    """Pre-mixed flat blend of RGB `a` toward `b` by t (0..1). Never raises.

    Used for fake-alpha fills (e.g. chip fills at 18% against BG) so no real
    alpha compositing is needed for flat colors."""
    try:
        t = float(t)
    except Exception:
        t = 0.0
    if t != t:  # NaN
        t = 0.0
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# --- Font handling ---------------------------------------------------------
_FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
# Kept for back-compat with older callers/tests.
_FONT_FILES = {
    "regular": _FONT_DIR + "DejaVuSans.ttf",
    "bold": _FONT_DIR + "DejaVuSans-Bold.ttf",
    "mono": _FONT_DIR + "DejaVuSansMono-Bold.ttf",
}

# Candidate (path, ttc_index, variation) lists per weight: the HUD faces from
# the mockup (Saira display, IBM Plex Mono data) first, then DejaVu (Pi
# fallback), then macOS build-host faces so test renders approximate the
# deployed look. `variation` (or None) names a variable-font instance and is
# applied best-effort after load.
_HUD_FONT_DIR = "/usr/share/fonts/truetype/rubyhud/"
_FONT_CANDIDATES = {
    "regular": (
        (_HUD_FONT_DIR + "Saira.ttf", 0, "Medium"),
        (_FONT_FILES["regular"], 0, None),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", 0, None),
        ("/System/Library/Fonts/Helvetica.ttc", 0, None),
    ),
    "bold": (
        (_HUD_FONT_DIR + "Saira.ttf", 0, "SemiBold"),
        (_FONT_FILES["bold"], 0, None),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0, None),
        ("/System/Library/Fonts/Helvetica.ttc", 1, None),
    ),
    "mono": (
        (_HUD_FONT_DIR + "IBMPlexMono-SemiBold.ttf", 0, None),
        (_FONT_FILES["mono"], 0, None),
        ("/System/Library/Fonts/Menlo.ttc", 1, None),
        ("/System/Library/Fonts/Monaco.ttf", 0, None),
    ),
}

_font_cache: dict = {}


def font(size: int, weight: str = "regular"):
    """Return a cached ImageFont for (size, weight).

    weight in {'regular','bold','mono'}. Tries each candidate face in order,
    then a sized PIL default, then the bare default. Never raises.
    """
    try:
        size = int(size)
    except Exception:
        size = 12
    if size < 1:
        size = 1
    if weight not in _FONT_CANDIDATES:
        weight = "regular"
    key = (size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    f = None
    for path, idx, variation in _FONT_CANDIDATES[weight]:
        try:
            f = ImageFont.truetype(path, size, index=idx)
            if variation:
                try:
                    f.set_variation_by_name(variation)
                except Exception:
                    pass
            break
        except Exception:
            f = None
    if f is None:
        # Try a sized default first (Pillow >= 9.2 supports size arg), then
        # the bare default. Either way, never raise.
        try:
            f = ImageFont.load_default(size=size)
        except Exception:
            try:
                f = ImageFont.load_default()
            except Exception:
                f = None
    _font_cache[key] = f
    return f
