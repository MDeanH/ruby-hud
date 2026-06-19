"""Reusable touch menu widget (TouchMenu + MenuItem).

TouchMenu is a Page whose body is a card with a breadcrumb title band and a
scrollable list of MenuItems; SettingsPage subclasses it. A MenuItem either
runs on_tap(ctx), pushes a submenu (list or callable -> list), or opens a
confirm modal that runs on_tap only after CONFIRM. Rows show a live value
(value_fn, re-evaluated per frame), dim when enabled_fn() is False, and the
list lives between MENU_X0/X1 so the global 130/1150 edge tap zones stay
clear.

Gestures: tap hits rows / modal buttons; hold pops the stack (or closes the
modal); vertical swipes scroll by 3 rows. The static layer holds only the
outer card frame + title band (never invalidated); everything else -
breadcrumb, rows, values, scrollbar, modal - is drawn per frame.
"""

from __future__ import annotations

import time

from PIL import Image

from . import gauges
from .pages import Page
from .render import BOT_SEP_Y, SS, TOP_STRIP_H
from .theme import (ACCENT, BG, CARD_BORDER, DANGER, PANEL, TEXT, TEXT_DIM,
                    TICK, font, mix)

# Dim-overlay sprites for the modal (one per size; trivially small cache).
_DIM_CACHE: dict = {}


def _dim_sprite(w: int, h: int) -> Image.Image:
    key = (int(w), int(h))
    img = _DIM_CACHE.get(key)
    if img is None:
        img = Image.new("RGBA", key, (0, 0, 0, 150))
        _DIM_CACHE[key] = img
    return img


def _vtext(draw, x, cy, text, fnt, fill, right=False):
    """Vertically-centered row text, left- (or right-) aligned at x."""
    txt = str(text)
    w, h = gauges._text_size(draw, txt, fnt)
    try:
        l, t, _, _ = draw.textbbox((0, 0), txt, font=fnt)
    except Exception:
        l, t = 0, 0
    draw.text((x - w - l if right else x - l, cy - h / 2 - t), txt,
              font=fnt, fill=fill)


def _wrap2(draw, text, fnt, max_w):
    """Word-wrap text into at most two lines that fit max_w (best effort)."""
    txt = str(text)
    if gauges._text_size(draw, txt, fnt)[0] <= max_w:
        return [txt]
    words = txt.split()
    lines = [txt]
    for i in range(1, len(words)):
        head = " ".join(words[:i])
        if gauges._text_size(draw, head, fnt)[0] <= max_w:
            lines = [head, " ".join(words[i:])]
    return lines


class MenuItem:
    """One menu row: static label + live value, tap/submenu/confirm action.

    confirm may be a str or a callable returning one (for dynamic messages
    like 'Install v3.1.0?'); submenu may be a list[MenuItem] or a callable
    returning one (re-resolved on every push)."""

    __slots__ = ("label", "value_fn", "on_tap", "submenu", "enabled_fn",
                 "danger", "confirm")

    def __init__(self, label, *, value_fn=None, on_tap=None, submenu=None,
                 enabled_fn=None, danger=False, confirm=None):
        self.label = str(label)
        self.value_fn = value_fn
        self.on_tap = on_tap
        self.submenu = submenu
        self.enabled_fn = enabled_fn
        self.danger = bool(danger)
        self.confirm = confirm

    def enabled(self) -> bool:
        if self.enabled_fn is None:
            return True
        try:
            return bool(self.enabled_fn())
        except Exception:
            return False

    def value(self):
        if self.value_fn is None:
            return None
        try:
            v = self.value_fn()
        except Exception:
            return None
        return None if v is None else str(v)

    def confirm_text(self):
        c = self.confirm
        if callable(c):
            try:
                c = c()
            except Exception:
                c = None
        return None if c is None else str(c)

    def submenu_items(self) -> list:
        sub = self.submenu
        if callable(sub):
            try:
                sub = sub()
            except Exception:
                sub = []
        return list(sub or [])


class TouchMenu(Page):
    name = "MENU"

    # List geometry (screen px, pre-supersample). The card stays clear of
    # the 130/1150 edge tap zones handled by __main__.
    MENU_X0, MENU_X1 = 160, 1120
    LIST_Y0, ROW_H, N_VIS = 150, 74, 7
    CARD_Y0, CARD_Y1 = 92, 712
    TITLE_H = 48           # breadcrumb band height inside the card

    # Confirm modal geometry (screen px; also the tap hit boxes).
    MODAL_X0, MODAL_X1 = 320, 960
    MODAL_Y0, MODAL_Y1 = 290, 530
    # Fatter buttons for an in-car touchscreen (436 + 76 = 512 < MODAL_Y1 530).
    BTN_W, BTN_H, BTN_GAP = 250, 76, 60
    BTN_Y0 = 436

    FLASH_S = 0.25         # row highlight time after a tap
    MODAL_TTL = 30.0       # auto-dismiss an unattended confirm modal

    def __init__(self, title=None, items=None):
        self.stack = [(str(title or self.name), list(items or []))]
        self.scroll = 0
        self.modal = None  # {"message", "on_tap", "danger"} | None
        self.flash = None  # (slot, t_monotonic) | None

    # -- stack -------------------------------------------------------------- #
    def push(self, title, items) -> None:
        self.stack.append((str(title), list(items or [])))
        self.scroll = 0
        self.flash = None

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
        self.scroll = 0
        self.flash = None

    def _modal_live(self) -> bool:
        """True while a confirm modal is open and not yet auto-expired.

        Edge taps / horizontal swipes are routed by __main__ before the page
        sees them, so the user can page away with a modal armed; without a TTL
        it would wait indefinitely, one stray tap from executing a stale
        Install/Rollback/Console-dash action."""
        m = self.modal
        if m is None:
            return False
        t0 = m.get("t0")
        if t0 is not None and time.monotonic() - t0 >= self.MODAL_TTL:
            self.modal = None
            return False
        return True

    def _capacity(self) -> int:
        """Item slots per screen (one less when the BACK row is pinned)."""
        return self.N_VIS - (1 if len(self.stack) > 1 else 0)

    def _max_scroll(self) -> int:
        return max(0, len(self.stack[-1][1]) - self._capacity())

    def _rows(self) -> list:
        """Visible rows as (slot, kind, payload); kind 'back' | 'item'."""
        items = self.stack[-1][1]
        out = []
        slot = 0
        if len(self.stack) > 1:
            out.append((0, "back", None))
            slot = 1
        start = max(0, min(self.scroll, self._max_scroll()))
        for i, item in enumerate(items[start:start + self._capacity()]):
            out.append((slot + i, "item", item))
        return out

    # -- rendering ------------------------------------------------------------ #
    def render_static(self, draw, img):
        x0, x1 = self.MENU_X0 * SS, self.MENU_X1 * SS
        # Flat Tesla menu: no card panel, just a hairline under the breadcrumb.
        ty = (self.CARD_Y0 + self.TITLE_H) * SS
        draw.line([(x0, ty), (x1, ty)], fill=mix(BG, CARD_BORDER, 0.6), width=SS)

    def render(self, draw, img, snap, ctx):
        self.scroll = max(0, min(self.scroll, self._max_scroll()))
        self._draw_breadcrumb(draw)
        self._draw_rows(draw)
        if self._max_scroll() > 0:
            self._draw_scrollbar(draw)
        # Footer hint is dynamic so progress/result panels do not show it, and
        # only names gestures that actually do something here: BACK only when
        # there's a stack to pop, SCROLL only when the screen overflows. Menus
        # are now sized to fit, so at the root this footer is simply absent.
        parts = []
        if len(self.stack) > 1:
            parts.append("HOLD = BACK")
        if self._max_scroll() > 0:
            parts.append("SWIPE = SCROLL")
        if parts:
            gauges.tracked_text_center(
                draw, ((self.MENU_X0 + self.MENU_X1) // 2) * SS, 694 * SS,
                "   ".join(parts), font(14 * SS, "bold"),
                mix(BG, TEXT_DIM, 0.6), tracking=2 * SS)
        if self._modal_live():
            self._draw_modal(draw, img)

    def _draw_breadcrumb(self, draw):
        crumb = " > ".join(t for t, _ in self.stack)
        gauges.tracked_text(draw, (self.MENU_X0 + 28) * SS,
                            (self.CARD_Y0 + 13) * SS, crumb,
                            font(20 * SS, "bold"), TEXT_DIM, tracking=2 * SS)

    def _draw_rows(self, draw):
        now = time.monotonic()
        x0, x1 = self.MENU_X0 * SS, self.MENU_X1 * SS
        lfont = font(27 * SS, "regular")
        vfont = font(22 * SS, "regular")
        rows = self._rows()
        for slot, kind, item in rows:
            ry = (self.LIST_Y0 + slot * self.ROW_H) * SS
            cy = ry + (self.ROW_H * SS) // 2
            if (self.flash is not None and self.flash[0] == slot
                    and now - self.flash[1] < self.FLASH_S):
                draw.rounded_rectangle(
                    [x0 + 10 * SS, ry + 3 * SS,
                     x1 - 10 * SS, ry + (self.ROW_H - 3) * SS],
                    radius=10 * SS, fill=mix(PANEL, ACCENT, 0.22))
            if slot < self.N_VIS - 1:  # separator under all but the last row
                sy = ry + self.ROW_H * SS
                draw.line([(x0 + 24 * SS, sy), (x1 - 24 * SS, sy)],
                          fill=mix(PANEL, CARD_BORDER, 0.6), width=SS)
            if kind == "back":
                _vtext(draw, (self.MENU_X0 + 32) * SS, cy, "<  BACK",
                       font(24 * SS, "bold"), TEXT_DIM)
                continue
            enabled = item.enabled()
            lcol = DANGER if item.danger else TEXT
            vcol = TEXT_DIM
            if not enabled:
                lcol = mix(BG, lcol, 0.45)
                vcol = mix(BG, vcol, 0.45)
            _vtext(draw, (self.MENU_X0 + 32) * SS, cy, item.label, lfont,
                   lcol)
            vx = self.MENU_X1 - 32
            if item.submenu is not None:
                gauges._centered_text(draw, (self.MENU_X1 - 40) * SS, cy,
                                      ">", font(26 * SS, "bold"), vcol)
                vx = self.MENU_X1 - 68
            value = item.value()
            if value is not None:
                _vtext(draw, vx * SS, cy, value, vfont, vcol, right=True)

    def _draw_scrollbar(self, draw):
        n = max(1, len(self.stack[-1][1]))
        x = (self.MENU_X1 - 14) * SS
        y0 = self.LIST_Y0 * SS
        y1 = (self.LIST_Y0 + self.N_VIS * self.ROW_H) * SS
        draw.rounded_rectangle([x, y0, x + 4 * SS, y1], radius=2 * SS,
                               fill=mix(BG, TICK, 0.5))
        th = max(24 * SS, int((y1 - y0) * self._capacity() / float(n)))
        ty = y0 + int((y1 - y0 - th)
                      * (self.scroll / float(max(1, self._max_scroll()))))
        draw.rounded_rectangle([x, ty, x + 4 * SS, ty + th], radius=2 * SS,
                               fill=TEXT_DIM)

    def _modal_buttons(self):
        """((x0,y0,x1,y1) cancel, confirm) hit boxes in screen px."""
        cx = (self.MODAL_X0 + self.MODAL_X1) // 2
        y0, y1 = self.BTN_Y0, self.BTN_Y0 + self.BTN_H
        cancel = (cx - self.BTN_GAP // 2 - self.BTN_W, y0,
                  cx - self.BTN_GAP // 2, y1)
        confirm = (cx + self.BTN_GAP // 2, y0,
                   cx + self.BTN_GAP // 2 + self.BTN_W, y1)
        return cancel, confirm

    def _draw_modal(self, draw, img):
        # Dim everything between the global strips, then the modal card.
        oy = (TOP_STRIP_H + 1) * SS
        dim = _dim_sprite(img.width, BOT_SEP_Y * SS - oy)
        img.paste(dim, (0, oy), dim)

        x0, y0 = self.MODAL_X0 * SS, self.MODAL_Y0 * SS
        x1, y1 = self.MODAL_X1 * SS, self.MODAL_Y1 * SS
        gauges.card(draw, x0, y0, x1, y1, radius=16 * SS, scale=SS)
        cx = (x0 + x1) // 2
        gauges.tracked_text_center(draw, cx, (self.MODAL_Y0 + 30) * SS,
                                   "CONFIRM", font(17 * SS, "bold"),
                                   TEXT_DIM, tracking=4 * SS)
        mfont = font(26 * SS, "regular")
        lines = _wrap2(draw, self.modal.get("message", ""), mfont,
                       (self.MODAL_X1 - self.MODAL_X0 - 80) * SS)
        my = (self.MODAL_Y0 + 78) * SS
        for line in lines[:2]:
            gauges._centered_text(draw, cx, my, line, mfont, TEXT)
            my += 38 * SS

        cancel, confirm = self._modal_buttons()
        bfont = font(22 * SS, "bold")
        draw.rounded_rectangle([v * SS for v in cancel], radius=12 * SS,
                               fill=mix(BG, TEXT_DIM, 0.12),
                               outline=mix(BG, TEXT_DIM, 0.5),
                               width=max(1, SS))
        gauges._centered_text(draw, (cancel[0] + cancel[2]) * SS // 2,
                              (cancel[1] + cancel[3]) * SS // 2, "CANCEL",
                              bfont, TEXT_DIM)
        ccol = DANGER if self.modal.get("danger") else ACCENT
        draw.rounded_rectangle([v * SS for v in confirm], radius=12 * SS,
                               fill=ccol)
        gauges._centered_text(draw, (confirm[0] + confirm[2]) * SS // 2,
                              (confirm[1] + confirm[3]) * SS // 2, "CONFIRM",
                              bfont, (255, 255, 255))

    # -- input ---------------------------------------------------------------- #
    @staticmethod
    def _hit(rect, x, y) -> bool:
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def handle_tap(self, x, y, ctx):
        if self._modal_live():
            return self._tap_modal(x, y, ctx)
        if not (self.MENU_X0 <= x <= self.MENU_X1):
            return False
        if not (self.LIST_Y0 <= y < self.LIST_Y0 + self.N_VIS * self.ROW_H):
            return False
        slot = int((y - self.LIST_Y0) // self.ROW_H)
        for s, kind, item in self._rows():
            if s != slot:
                continue
            self.flash = (slot, time.monotonic())
            if kind == "back":
                self.pop()
                return True
            return self._tap_item(item, ctx)
        return True  # empty slot inside the list: consume, no-op

    def _tap_item(self, item, ctx):
        if not item.enabled():
            return True
        if item.submenu is not None:
            self.push(item.label, item.submenu_items())
            return True
        msg = item.confirm_text()
        if msg is not None and item.on_tap is not None:
            self.modal = {"message": msg, "on_tap": item.on_tap,
                          "danger": item.danger, "t0": time.monotonic()}
            return True
        if item.on_tap is not None:
            try:
                item.on_tap(ctx)
            except Exception:
                pass
        return True

    def _tap_modal(self, x, y, ctx):
        cancel, confirm = self._modal_buttons()
        modal = self.modal
        if self._hit(confirm, x, y):
            self.modal = None
            fn = modal.get("on_tap")
            if fn is not None:
                try:
                    fn(ctx)
                except Exception:
                    pass
        elif self._hit(cancel, x, y):
            self.modal = None
        return True  # modal swallows every tap

    def handle_hold(self, x, y, ctx):
        if self._modal_live():
            self.modal = None
            return True
        if len(self.stack) > 1:
            self.pop()
            return True
        return False

    def handle_swipe_v(self, direction, ctx):
        if self._modal_live():
            return True
        step = 3 if direction == "up" else -3
        self.scroll = max(0, min(self.scroll + step, self._max_scroll()))
        return True
