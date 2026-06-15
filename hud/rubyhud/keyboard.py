"""On-screen touch keyboard for rubyhud (Pillow-drawn, hit-tested in screen px).

A self-contained widget: render(draw, img) paints the keys at supersample
scale; handle_tap(x, y) takes *screen* pixels (the same space touch.py emits)
and mutates the buffer. The owner reads `.text`. Two layers (letters + a
symbol set rich enough for WPA passwords); shift is a sticky caps toggle.

Geometry matches the approved mockup (108x84 keycaps). All colors come from
theme.py so the widget follows whatever scheme is active -- nothing hardcoded.
Never raises.
"""

from __future__ import annotations

import time

from . import gauges
from .render import SS
from .theme import ACCENT, CARD_BORDER, PANEL, TEXT, TEXT_DIM, font, mix

# Layout (screen px, pre-supersample).
KEY_H = 84
GAP = 12
KB_TOP = 300
ROW_PITCH = KEY_H + 14            # 98
KEY_W = 108
KB_BOTTOM = KB_TOP + 3 * ROW_PITCH + KEY_H   # 678
FLASH_S = 0.12

# Character rows per layer: (row0=10, row1=9, row2=7). The skeleton (shift +
# 7 + backspace, then layer + space + dot + enter) is identical across layers
# so key positions never move; shift is a no-op on the symbol layer.
_LAYERS = {
    "abc": ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
    "sym": ("1234567890", "@#$%&*-_+", "=()/:;!"),
}


class Keyboard:
    def __init__(self):
        self.text = ""
        self._layer = "abc"
        self._shift = False
        self._flash = None          # (x, y, w, h, t_monotonic)

    def reset(self):
        self.text = ""
        self._layer = "abc"
        self._shift = False
        self._flash = None

    # -- key geometry ----------------------------------------------------- #
    def _keys(self) -> list:
        """List of key dicts {x,y,w,h,kind,char,label} for the active layer."""
        r0, r1, r2 = _LAYERS[self._layer]
        keys = []

        def add(x, y, w, kind, char=None, label=None):
            keys.append({"x": x, "y": y, "w": w, "h": KEY_H, "kind": kind,
                         "char": char, "label": label})

        y0 = KB_TOP
        for i, c in enumerate(r0):
            add(46 + i * (KEY_W + GAP), y0, KEY_W, "char", c)
        y1 = KB_TOP + ROW_PITCH
        for i, c in enumerate(r1):
            add(106 + i * (KEY_W + GAP), y1, KEY_W, "char", c)
        y2 = KB_TOP + 2 * ROW_PITCH
        add(64, y2, 150, "shift")
        for i, c in enumerate(r2):
            add(226 + i * (KEY_W + GAP), y2, KEY_W, "char", c)
        add(1066, y2, 150, "backspace")
        y3 = KB_TOP + 3 * ROW_PITCH
        add(64, y3, 150, "layer", label="?123" if self._layer == "abc" else "ABC")
        add(226, y3, 720, "space")
        add(958, y3, KEY_W, "char", ".")
        add(1078, y3, 138, "enter")
        return keys

    def _shifted(self, c: str) -> str:
        return c.upper() if (self._shift and self._layer == "abc") else c

    # -- input ------------------------------------------------------------ #
    def handle_tap(self, x, y):
        """(x, y) in screen px. Returns 'type' | 'enter' | None (no hit)."""
        for k in self._keys():
            if k["x"] <= x <= k["x"] + k["w"] and k["y"] <= y <= k["y"] + k["h"]:
                self._flash = (k["x"], k["y"], k["w"], k["h"], time.monotonic())
                kind = k["kind"]
                if kind == "char":
                    self.text += self._shifted(k["char"])
                    return "type"
                if kind == "space":
                    self.text += " "
                    return "type"
                if kind == "backspace":
                    self.text = self.text[:-1]
                    return "type"
                if kind == "shift":
                    if self._layer == "abc":
                        self._shift = not self._shift
                    return "type"
                if kind == "layer":
                    self._layer = "sym" if self._layer == "abc" else "abc"
                    self._shift = False
                    return "type"
                if kind == "enter":
                    return "enter"
                return None
        return None

    # -- render ----------------------------------------------------------- #
    def render(self, draw, img):
        now = time.monotonic()
        keycap = mix(PANEL, CARD_BORDER, 0.7)
        special = PANEL
        for k in self._keys():
            x0, y0 = k["x"] * SS, k["y"] * SS
            x1, y1 = (k["x"] + k["w"]) * SS, (k["y"] + k["h"]) * SS
            kind = k["kind"]
            flashed = (self._flash is not None
                       and self._flash[0] == k["x"] and self._flash[1] == k["y"]
                       and now - self._flash[4] < FLASH_S)
            if kind == "enter":
                fill = mix(ACCENT, TEXT, 0.18) if flashed else ACCENT
            elif kind == "char":
                fill = mix(keycap, ACCENT, 0.45) if flashed else keycap
            else:
                fill = mix(special, ACCENT, 0.45) if flashed else special
            if kind == "shift" and self._shift and self._layer == "abc":
                fill = mix(special, ACCENT, 0.5)
            draw.rounded_rectangle([x0, y0, x1, y1], radius=9 * SS, fill=fill,
                                   outline=CARD_BORDER, width=SS)
            cx = (k["x"] + k["w"] / 2) * SS
            cy = (k["y"] + k["h"] / 2) * SS
            if kind == "char":
                gauges._centered_text(draw, cx, cy, self._shifted(k["char"]),
                                      font(36 * SS, "regular"), TEXT)
            elif kind == "space":
                gauges.tracked_text_center(draw, cx, cy, "space",
                                           font(18 * SS, "bold"), TEXT_DIM,
                                           tracking=3 * SS)
            elif kind == "layer":
                gauges._centered_text(draw, cx, cy, k["label"],
                                      font(24 * SS, "bold"), TEXT_DIM)
            elif kind == "shift":
                self._glyph_shift(draw, cx, cy)
            elif kind == "backspace":
                self._glyph_backspace(draw, cx, cy)
            elif kind == "enter":
                self._glyph_enter(draw, cx, cy)

    # -- special-key glyphs (hand-drawn, like the rest of the HUD) -------- #
    @staticmethod
    def _glyph_shift(draw, cx, cy):
        col = TEXT_DIM
        u = SS
        draw.polygon([(cx, cy - 16 * u), (cx - 16 * u, cy + 2 * u),
                      (cx - 7 * u, cy + 2 * u), (cx - 7 * u, cy + 14 * u),
                      (cx + 7 * u, cy + 14 * u), (cx + 7 * u, cy + 2 * u),
                      (cx + 16 * u, cy + 2 * u)], fill=col)

    @staticmethod
    def _glyph_backspace(draw, cx, cy):
        col = TEXT_DIM
        u = SS
        draw.polygon([(cx - 18 * u, cy), (cx - 4 * u, cy - 13 * u),
                      (cx + 18 * u, cy - 13 * u), (cx + 18 * u, cy + 13 * u),
                      (cx - 4 * u, cy + 13 * u)], outline=col, width=max(1, 2 * u))
        draw.line([(cx, cy - 6 * u), (cx + 12 * u, cy + 6 * u)], fill=col,
                  width=max(1, 2 * u))
        draw.line([(cx + 12 * u, cy - 6 * u), (cx, cy + 6 * u)], fill=col,
                  width=max(1, 2 * u))

    @staticmethod
    def _glyph_enter(draw, cx, cy):
        col = (255, 255, 255)
        u = SS
        draw.line([(cx + 14 * u, cy - 12 * u), (cx + 14 * u, cy),
                   (cx - 12 * u, cy)], fill=col, width=max(1, 3 * u), joint="curve")
        draw.line([(cx - 12 * u, cy), (cx - 2 * u, cy - 8 * u)], fill=col,
                  width=max(1, 3 * u))
        draw.line([(cx - 12 * u, cy), (cx - 2 * u, cy + 8 * u)], fill=col,
                  width=max(1, 3 * u))
