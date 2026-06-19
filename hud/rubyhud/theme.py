"""Theme constants and font loading for rubyhud.

Palette colors are RGB tuples ("premium cluster" palette: deep charcoal with
Soul Red accents). Old constant names are kept as aliases of the new palette
so existing callers keep working. Fonts are loaded from DejaVu TTFs with safe
fallbacks (macOS build-host faces, then PIL's default) and never raise.
"""

from PIL import ImageFont

# --- Palette (RGB) ---------------------------------------------------------
# Colors are organized as named *schemes* so a future MENU picker can
# offer alternates. "soul-red" is the default, bound to Michael's "Mazda HUD"
# mockup (Downloads/Mazda HUD (standalone).html): bg #07090c, ring track
# #2a3340, bright track #4a5666, accent #d0273b, dim text #8d99a7.
#
# ARCHITECTURE NOTE (picker is a later release): every scheme dict carries the
# exact same keys, and _bind() pushes the active scheme's values into the
# module-level constants below. Adding a scheme = adding a dict entry. The
# remaining work for a live in-HUD picker is (a) persisting the choice in
# config.py, and (b) making callers read theme.X dynamically + clearing the
# render.py static caches on switch -- because most modules do
# `from .theme import ACCENT, ...` which binds once at import. New UI (e.g.
# the WiFi page) imports tokens the same way, so it is automatically correct
# for whichever scheme is active at process start.
_SCHEMES = {
    "soul-red": {                    # default (Mazda HUD mockup)
        "BG": (7, 9, 12),            # 07090C deep charcoal (bottom of ramp)
        "BG_TOP": (12, 16, 22),      # 0C1016 top of the luminance ramp
        "PANEL": (16, 20, 27),       # 10141B card fill
        "CARD": (16, 20, 27),        # alias of PANEL
        "CARD_BORDER": (42, 51, 64),  # 2A3340 1px card border (ring track)
        "CARD_EDGE": (58, 68, 84),   # 3A4454 lighter top-edge highlight
        "ACCENT": (208, 39, 59),     # D0273B Soul Red
        "ACCENT_GLOW": (255, 77, 92),  # FF4D5C brighter glow tone
        "ACCENT_DIM": (104, 20, 30),  # deep red (gradient tails)
        "DANGER": (255, 59, 48),     # FF3B30
        "WARN": (255, 179, 0),       # FFB300
        "OK": (46, 204, 113),        # 2ECC71
        "TEXT": (243, 247, 251),     # F3F7FB bright text
        "TEXT_DIM": (141, 153, 167),  # 8D99A7 dim text
        "TICK": (42, 51, 64),        # 2A3340 gauge tracks / ticks
        "TICK_BRIGHT": (74, 86, 102),  # 4A5666 brighter track
        "NEEDLE": (245, 247, 250),   # needle color
        "ROW_A": (15, 19, 25),       # 0F1319 zebra row (CAN page)
        "ROW_B": (11, 14, 19),       # 0B0E13 zebra row
    },
    "ion-blue": {                    # alternate (cool) -- groundwork, not yet
        "BG": (7, 10, 14),           # selectable; here to prove the registry.
        "BG_TOP": (12, 17, 24),
        "PANEL": (16, 22, 30),
        "CARD": (16, 22, 30),
        "CARD_BORDER": (40, 54, 68),
        "CARD_EDGE": (56, 72, 92),
        "ACCENT": (38, 138, 221),    # 268ADD
        "ACCENT_GLOW": (90, 178, 255),
        "ACCENT_DIM": (18, 60, 100),
        "DANGER": (255, 59, 48),
        "WARN": (255, 179, 0),
        "OK": (46, 204, 113),
        "TEXT": (243, 247, 251),
        "TEXT_DIM": (139, 153, 167),
        "TICK": (40, 54, 68),
        "TICK_BRIGHT": (72, 92, 112),
        "NEEDLE": (245, 247, 250),
        "ROW_A": (14, 19, 26),
        "ROW_B": (10, 14, 20),
    },
}
_ACTIVE_SCHEME = "soul-red"


def scheme_names() -> list:
    """Names of the available color schemes (for a future picker)."""
    return list(_SCHEMES)


def active_scheme() -> str:
    return _ACTIVE_SCHEME


def _bind(name: str) -> None:
    """Push a scheme's values into the module-level color constants."""
    p = _SCHEMES.get(name) or _SCHEMES["soul-red"]
    globals().update(p)
    globals()["_ACTIVE_SCHEME"] = name if name in _SCHEMES else "soul-red"


def apply_scheme(name: str) -> None:
    """Rebind the active scheme. NOTE: only affects attribute access via
    `theme.X` afterwards; modules that did `from .theme import ACCENT` keep
    their import-time binding, and render.py's static caches must be cleared.
    Wiring those is the picker's follow-up work (see ARCHITECTURE NOTE)."""
    _bind(name)


# Bind the default scheme so the constants (BG, PANEL, ACCENT, ...) exist at
# import for every `from .theme import ...` caller.
_bind(_ACTIVE_SCHEME)


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
    # Light/thin instance of the variable Saira — the hero numerals (speed,
    # gear) use this for the Tesla hairline look. Falls back to DejaVu/macOS
    # regular on the bench (heavier than the deployed Pi render).
    "thin": (
        (_HUD_FONT_DIR + "Saira.ttf", 0, "Light"),
        (_FONT_FILES["regular"], 0, None),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", 0, None),
        ("/System/Library/Fonts/Helvetica.ttc", 0, None),
    ),
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
