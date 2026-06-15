"""BLUETOOTH page: scan, pair (just-works), connect, disconnect, forget.

Hidden page (name "BLUETOOTH"), reached via CONFIGURE > BLUETOOTH > MANAGE / PAIR
(ctx['nav_request'] = "BLUETOOTH"). All bluetoothctl work goes through btnet,
which runs every slow op on a background thread -- this page only reads caches
and draws, so the 15 fps loop never stalls. v1 pairing is just-works/SSP (no PIN
UX). While the page is open the controller is made discoverable+pairable so a
phone can also pair TO the Pi.

States: list | working | result | actions(sheet). Hold or a horizontal swipe
leaves the page (handled by the nav layer); hold also backs out of a sub-state
first. Colors come from theme.py (scheme-aware). Never raises into the loop.
"""

from __future__ import annotations

import time

from . import btnet, gauges, theme
from .menu import _vtext
from .pages import Page
from .render import BOT_SEP_Y, SS, SW, TOP_STRIP_H
from .theme import (ACCENT, ACCENT_GLOW, BG, CARD_BORDER, DANGER, OK, PANEL,
                    TEXT, TEXT_DIM, WARN, font, mix)


def _vt(draw, x, y, text, fnt, fill, right=False):
    """_vtext (vertically-centered row text) in screen-px coords, scaled into
    the SS draw space. Fonts passed in are already SS-sized."""
    _vtext(draw, x * SS, y * SS, text, fnt, fill, right=right)


AREA_X0, AREA_X1 = 28, 1252

# list-mode geometry (screen px)
CC = (28, 100, 1252, 176)                  # controller / connected summary card
RESCAN_BTN = (1092, 188, 1232, 228)
ROW_TOP, ROW_H, N_VIS = 234, 80, 6

_WORK_VIEW_MIN = 0.6        # keep the working screen up at least this long
_WORK_TIMEOUT = 42.0        # backstop above btnet's ~35s pair session timeout


class BluetoothPage(Page):
    name = "BLUETOOTH"
    hidden = True

    def __init__(self):
        self._mode = "list"
        self._scroll = 0
        self._sheet = None          # {"dev","buttons":[(label,kind)]}
        self._work_t0 = 0.0
        self._work_label = "CONNECTING"

    # -- lifecycle -------------------------------------------------------- #
    def on_show(self, ctx):
        self._mode = "list"
        self._scroll = 0
        self._sheet = None
        # Always scan on open: BlueZ prunes discovered (unpaired) devices, so a
        # fresh scan is needed to see what's nearby. rescan() also does a quick
        # pre-scan read so paired devices appear immediately. (poke()-then-
        # rescan() would race on the shared refresh guard and drop the scan.)
        btnet.rescan()
        btnet.set_pairable(True)    # let phones pair TO the Pi while open

    def on_hide(self, ctx):
        self._mode = "list"
        self._sheet = None
        btnet.set_pairable(False)

    # -- render ----------------------------------------------------------- #
    def render_static(self, draw, img):
        pass

    def render(self, draw, img, snap, ctx):
        if self._mode == "list":
            btnet.poke()
            self._render_list(draw)
        elif self._mode == "working":
            self._render_working(draw)
            self._advance_working()
        elif self._mode == "result":
            self._render_result(draw)
        if self._sheet is not None:
            self._render_sheet(draw, img)

    def _crumb(self, draw, tail=""):
        txt = "BLUETOOTH" + ("  ›  " + tail if tail else "")
        gauges.tracked_text(draw, 40 * SS, 80 * SS, txt,
                            font(25 * SS, "bold"), TEXT_DIM, tracking=3 * SS)

    # -- list ------------------------------------------------------------- #
    def _render_list(self, draw):
        self._crumb(draw)
        self._draw_summary(draw)
        gauges.tracked_text(draw, AREA_X0 * SS, 217 * SS, "PAIRED & NEARBY",
                            font(17 * SS, "bold"), TEXT_DIM, tracking=3 * SS)
        scanning = btnet.scanning()
        self._btn(draw, RESCAN_BTN, "SCANNING…" if scanning else "RESCAN",
                  None, ACCENT_GLOW if scanning else TEXT_DIM,
                  outline=CARD_BORDER)

        devs = btnet.devices()
        if not devs:
            gauges._centered_text(draw, SW // 2, 430 * SS,
                                  "SCANNING…" if scanning else "NO DEVICES",
                                  font(30 * SS, "bold"), TEXT_DIM)
        else:
            self._scroll = max(0, min(self._scroll, max(0, len(devs) - N_VIS)))
            shown = devs[self._scroll:self._scroll + N_VIS]
            for i, dev in enumerate(shown):
                self._draw_row(draw, ROW_TOP + i * ROW_H, dev)
            if len(devs) > N_VIS:
                self._draw_scrollbar(draw, len(devs))
        gauges.tracked_text_center(
            draw, SW // 2, 724 * SS,
            "TAP A DEVICE      SWIPE = SCROLL      HOLD = BACK",
            font(15 * SS, "bold"), mix(BG, TEXT_DIM, 0.65), tracking=2 * SS)

    def _draw_summary(self, draw):
        x0, y0, x1, y1 = (v * SS for v in CC)
        gauges.card(draw, x0, y0, x1, y1, radius=12 * SS, scale=SS)
        cy = (CC[1] + CC[3]) // 2
        devs = btnet.devices()
        conn = [d for d in devs if d.get("connected")]
        powered = btnet.status().get("powered", True)
        glyph_col = ACCENT_GLOW if (powered and conn) else (
            TEXT_DIM if powered else mix(TEXT, BG, 0.05))
        self._bt_glyph(draw, 52, cy, glyph_col, hh=18)
        if not powered:
            _vt(draw, 96, cy, "BLUETOOTH OFF", font(26 * SS, "regular"), TEXT_DIM)
        elif conn:
            gauges.tracked_text(draw, 96 * SS, (CC[1] + 24) * SS, "CONNECTED",
                                font(15 * SS, "bold"), OK, tracking=2 * SS)
            names = ", ".join(d.get("name") or "--" for d in conn)
            _vt(draw, 96, CC[1] + 52, names[:42],
                font(28 * SS, "regular"), TEXT)
            paired = sum(1 for d in devs if d.get("paired"))
            _vt(draw, 1232, cy, "%d paired" % paired,
                font(19 * SS, "regular"), TEXT_DIM, right=True)
        else:
            _vt(draw, 96, cy, "NO DEVICE CONNECTED",
                font(26 * SS, "regular"), TEXT_DIM)
            paired = sum(1 for d in devs if d.get("paired"))
            if paired:
                _vt(draw, 1232, cy, "%d paired" % paired,
                    font(19 * SS, "regular"), TEXT_DIM, right=True)

    def _draw_row(self, draw, top, dev):
        cy = top + ROW_H // 2
        if top > ROW_TOP:
            draw.line([(48 * SS, top * SS), (AREA_X1 * SS, top * SS)],
                      fill=mix(PANEL, CARD_BORDER, 0.6), width=SS)
        connected = dev.get("connected")
        paired = dev.get("paired")
        col = ACCENT_GLOW if connected else (
            TEXT if paired else mix(TEXT, BG, 0.05))
        self._bt_glyph(draw, 52, cy, col, hh=15)
        _vt(draw, 116, cy - 13, dev.get("name") or "--",
            font(27 * SS, "regular"), TEXT)
        sub = dev.get("mac") or ""
        if connected:
            sub = "connected  ·  " + sub
        elif paired:
            sub = "paired  ·  " + sub
        _vt(draw, 116, cy + 16, sub.lower(), font(16 * SS, "mono"), TEXT_DIM)
        right = AREA_X1
        if connected:
            right = self._tag(draw, right, cy, "CONNECTED", ACCENT,
                              (255, 255, 255))
        elif paired:
            right = self._tag(draw, right, cy, "PAIRED", None, TEXT_DIM,
                              outline=CARD_BORDER)
        else:
            _vt(draw, right - 8, cy, "tap to pair",
                font(16 * SS, "bold"), WARN, right=True)

    def _draw_scrollbar(self, draw, n):
        x = (AREA_X1 - 8) * SS
        y0, y1 = ROW_TOP * SS, (ROW_TOP + N_VIS * ROW_H) * SS
        draw.rounded_rectangle([x, y0, x + 5 * SS, y1], radius=2 * SS,
                               fill=mix(BG, theme.TICK, 0.5))
        th = max(40 * SS, int((y1 - y0) * N_VIS / float(n)))
        span = max(1, n - N_VIS)
        ty = y0 + int((y1 - y0 - th) * (self._scroll / float(span)))
        draw.rounded_rectangle([x, ty, x + 5 * SS, ty + th], radius=2 * SS,
                               fill=TEXT_DIM)

    # -- working / result ------------------------------------------------- #
    def _render_working(self, draw):
        self._crumb(draw, self._work_label.title())
        cs = btnet.action_state()
        name = cs.get("name") or "device"
        gauges._centered_text(draw, SW // 2, 320 * SS, self._work_label,
                              font(50 * SS, "bold"), TEXT)
        gauges._centered_text(draw, SW // 2, 372 * SS, "to %s" % name,
                              font(24 * SS, "regular"), TEXT_DIM)
        lit = int(time.monotonic() / 0.4) % 3
        for i in range(3):
            dx = SW // 2 + (i - 1) * 36 * SS
            col = ACCENT_GLOW if i == lit else mix(BG, TEXT_DIM, 0.5)
            r = 7 * SS
            draw.ellipse([dx - r, 430 * SS - r, dx + r, 430 * SS + r], fill=col)

    def _advance_working(self):
        cs = btnet.action_state()
        elapsed = time.monotonic() - self._work_t0
        if elapsed >= _WORK_VIEW_MIN and cs.get("state") in ("ok", "failed"):
            self._mode = "result"
        elif elapsed >= _WORK_TIMEOUT:
            self._mode = "result"   # never strand the user on the spinner

    def _render_result(self, draw):
        self._crumb(draw, self._work_label.title())
        cs = btnet.action_state()
        ok = cs.get("state") == "ok"
        good = ("PAIRED" if self._work_label == "PAIRING" else "CONNECTED")
        title, col = (good, OK) if ok else ("FAILED", DANGER)
        x0, x1 = (AREA_X0 + 60) * SS, (AREA_X1 - 60) * SS
        draw.rounded_rectangle([x0, 250 * SS, x1, 470 * SS], radius=16 * SS,
                               fill=mix(BG, col, 0.16),
                               outline=mix(BG, col, 0.6), width=2 * SS)
        gauges._centered_text(draw, SW // 2, 322 * SS, title,
                              font(44 * SS, "bold"), col)
        if ok:
            sub = cs.get("name") or "--"
        else:
            sub = (cs.get("error") or "try again, or pair from the device")[:64]
        gauges._centered_text(draw, SW // 2, 388 * SS, sub,
                              font(22 * SS, "regular"), TEXT_DIM)
        gauges._centered_text(draw, SW // 2, 452 * SS, "TAP TO CONTINUE",
                              font(17 * SS, "bold"), mix(BG, TEXT_DIM, 0.7))

    # -- action sheet ----------------------------------------------------- #
    def _open_sheet(self, dev):
        btns = []
        if dev.get("connected"):
            btns.append(("DISCONNECT", "disconnect"))
        elif dev.get("paired"):
            btns.append(("CONNECT", "connect"))
        else:
            btns.append(("PAIR", "pair"))
        if dev.get("paired"):
            btns.append(("FORGET", "forget"))
        btns.append(("CANCEL", "cancel"))
        self._sheet = {"dev": dev, "buttons": btns}

    def _sheet_button_rects(self):
        btns = self._sheet["buttons"]
        n = len(btns)
        x0, x1 = 360, 920
        gap, m = 16, 24
        bw = (x1 - x0 - 2 * m - (n - 1) * gap) // n
        by0, by1 = 470, 540
        rects = []
        for i in range(n):
            bx = x0 + m + i * (bw + gap)
            rects.append((bx, by0, bx + bw, by1))
        return rects

    def _render_sheet(self, draw, img):
        oy = (TOP_STRIP_H + 1) * SS
        from .menu import _dim_sprite
        dim = _dim_sprite(img.width, BOT_SEP_Y * SS - oy)
        img.paste(dim, (0, oy), dim)
        x0, y0, x1, y1 = 360 * SS, 250 * SS, 920 * SS, 540 * SS
        gauges.card(draw, x0, y0, x1, y1, radius=16 * SS, scale=SS)
        cx = (360 + 920) // 2 * SS
        gauges.tracked_text_center(draw, cx, 300 * SS, "DEVICE",
                                   font(16 * SS, "bold"), TEXT_DIM, tracking=4 * SS)
        name = (self._sheet["dev"].get("name") or "--")[:24]
        gauges._centered_text(draw, cx, 360 * SS, name,
                              font(30 * SS, "regular"), TEXT)
        for (label, kind), rect in zip(self._sheet["buttons"],
                                       self._sheet_button_rects()):
            danger = kind == "forget"
            accent = kind in ("connect", "pair", "disconnect")
            fill = (mix(BG, DANGER, 0.2) if danger
                    else ACCENT if accent else mix(BG, TEXT_DIM, 0.12))
            tcol = DANGER if danger else ((255, 255, 255) if accent else TEXT_DIM)
            self._btn(draw, rect, label, None if danger else fill, tcol,
                      outline=mix(BG, DANGER, 0.6) if danger else None)

    # -- input ------------------------------------------------------------ #
    def handle_tap(self, x, y, ctx):
        if self._sheet is not None:
            return self._tap_sheet(x, y)
        if self._mode == "list":
            return self._tap_list(x, y)
        if self._mode == "result":
            self._mode = "list"
            btnet.poke()
            return True
        return True  # working: swallow

    def _tap_list(self, x, y):
        if self._hit(RESCAN_BTN, x, y):
            btnet.rescan()
            return True
        devs = btnet.devices()
        if ROW_TOP <= y < ROW_TOP + N_VIS * ROW_H and devs:
            i = (y - ROW_TOP) // ROW_H + self._scroll
            if 0 <= i < len(devs):
                self._open_sheet(devs[i])
        return True

    def _tap_sheet(self, x, y):
        for (label, kind), rect in zip(self._sheet["buttons"],
                                       self._sheet_button_rects()):
            if self._hit(rect, x, y):
                dev = self._sheet["dev"]
                mac = dev.get("mac")
                name = dev.get("name")
                self._sheet = None
                if kind == "pair":
                    if btnet.pair(mac, name):
                        self._begin_working("PAIRING")
                elif kind == "connect":
                    if btnet.connect(mac, name):
                        self._begin_working("CONNECTING")
                elif kind == "disconnect":
                    btnet.disconnect(mac)
                    self._mode = "list"
                elif kind == "forget":
                    btnet.forget(mac)
                    self._mode = "list"
                return True
        self._sheet = None  # tap outside a button dismisses
        return True

    def _begin_working(self, label):
        self._work_label = label
        self._work_t0 = time.monotonic()
        self._mode = "working"

    def handle_hold(self, x, y, ctx):
        if self._sheet is not None:
            self._sheet = None
            return True
        if self._mode != "list":
            self._mode = "list"
            return True
        return False  # list + no sheet: let the nav layer exit the page

    def handle_swipe_v(self, direction, ctx):
        if self._mode != "list" or self._sheet is not None:
            return True
        devs = btnet.devices()
        step = 2 if direction == "up" else -2
        self._scroll = max(0, min(self._scroll + step, max(0, len(devs) - N_VIS)))
        return True

    # -- small drawing helpers -------------------------------------------- #
    @staticmethod
    def _hit(rect, x, y) -> bool:
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    @staticmethod
    def _btn(draw, rect, label, fill, textcol, outline=None):
        x0, y0, x1, y1 = (v * SS for v in rect)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=12 * SS,
                               fill=fill if fill is not None else None,
                               outline=outline, width=SS if outline else 0)
        gauges.tracked_text_center(draw, (rect[0] + rect[2]) // 2 * SS,
                                   (rect[1] + rect[3]) // 2 * SS, label,
                                   font(18 * SS, "bold"), textcol, tracking=1 * SS)

    @staticmethod
    def _bt_glyph(draw, cx, cy, col, hh=15):
        """The Bluetooth rune: a vertical spine with two crossing arms. cx/cy
        and hh are screen px; drawn into SS space."""
        hw = hh * 0.62
        u = SS
        top = (cx, cy - hh)
        bot = (cx, cy + hh)
        r_up = (cx + hw, cy - hh / 2)
        r_dn = (cx + hw, cy + hh / 2)
        l_up = (cx - hw, cy - hh / 2)
        l_dn = (cx - hw, cy + hh / 2)
        pts = [l_up, r_dn, bot, top, r_up, l_dn]
        draw.line([(p[0] * u, p[1] * u) for p in pts], fill=col,
                  width=max(2, 3 * u // 2), joint="curve")

    @staticmethod
    def _tag(draw, x_right, cy, text, fill, textcol, outline=None):
        tfont = font(15 * SS, "bold")
        tw = gauges._text_size(draw, text, tfont)[0]
        w = tw // SS + 28
        x0 = x_right - w
        draw.rounded_rectangle([x0 * SS, (cy - 15) * SS, x_right * SS,
                                (cy + 15) * SS], radius=8 * SS,
                               fill=fill, outline=outline,
                               width=SS if outline else 0)
        gauges.tracked_text_center(draw, (x0 + x_right) // 2 * SS, cy * SS,
                                   text, tfont, textcol, tracking=2 * SS)
        return x0 - 14
