"""WI-FI page: scan, connect (on-screen WPA password), manage saved networks.

Hidden page (name "WIFI"), reached via MENU > WI-FI > MANAGE / CONNECT
(ctx['nav_request'] = "WIFI"). All NetworkManager work goes through wifinet,
which runs every slow op on a background thread -- this page only reads caches
and draws, so the 15 fps loop never stalls. Password entry uses the shared
Pillow keyboard widget. Credentials are owned by NetworkManager; this page
never stores or logs one.

States: list | password | connecting | result | actions(sheet). Hold or a
horizontal swipe leaves the page (handled by the nav layer); hold also backs
out of a sub-state first. Colors come from theme.py (scheme-aware). Never
raises into the render loop.
"""

from __future__ import annotations

import time

from . import gauges, theme, wifinet
from .keyboard import Keyboard
from .menu import _vtext
from .pages import Page
from .render import BOT_SEP_Y, SS, SW, TOP_STRIP_H
from .theme import (ACCENT, ACCENT_GLOW, BG, CARD_BORDER, DANGER, OK, PANEL,
                    TEXT, TEXT_DIM, WARN, font, mix)


def _vt(draw, x, y, text, fnt, fill, right=False):
    """_vtext (vertically-centered row text) taking screen-px coords, scaled
    into the SS draw space. Fonts passed in are already SS-sized."""
    _vtext(draw, x * SS, y * SS, text, fnt, fill, right=right)


AREA_X0, AREA_X1 = 28, 1252

# list-mode geometry (screen px)
CC = (28, 100, 1252, 176)                 # current-connection card
HOTSPOT_BTN = (900, 188, 1076, 228)
RESCAN_BTN = (1092, 188, 1232, 228)
ROW_TOP, ROW_H, N_VIS = 234, 80, 6

# password-mode geometry
PW_FIELD = (28, 196, 788, 258)
SHOW_BTN = (804, 196, 934, 258)
CANCEL_BTN = (946, 196, 1076, 258)
CONNECT_BTN = (1088, 196, 1252, 258)

_CONNECT_VIEW_MIN = 0.6     # keep the connecting screen up at least this long
_CONNECT_TIMEOUT = 50.0     # backstop: resolve the spinner even if a worker
                            # never reports (sits above wifinet's ~45s timeout)


class WiFiPage(Page):
    name = "WIFI"
    hidden = True

    def __init__(self):
        self._mode = "list"
        self._scroll = 0
        self._kb = Keyboard()
        self._target = None          # network dict being joined (password mode)
        self._show_pw = False
        self._sheet = None           # {"title","ssid","buttons":[(label,kind)]}
        self._connect_t0 = 0.0

    # -- lifecycle -------------------------------------------------------- #
    def on_show(self, ctx):
        self._mode = "list"
        self._scroll = 0
        self._sheet = None
        self._show_pw = False
        self._kb.reset()
        wifinet.poke()
        if not wifinet.networks():
            wifinet.rescan()

    def on_hide(self, ctx):
        self._mode = "list"
        self._sheet = None
        self._kb.reset()           # never keep a typed password around

    # -- render ----------------------------------------------------------- #
    def render_static(self, draw, img):
        pass

    def render(self, draw, img, snap, ctx):
        if self._mode == "list":
            wifinet.poke()
            self._render_list(draw)
        elif self._mode == "password":
            self._render_password(draw)
        elif self._mode == "connecting":
            self._render_connecting(draw)
            self._advance_connecting()
        elif self._mode == "result":
            self._render_result(draw)
        if self._sheet is not None:
            self._render_sheet(draw, img)

    def _crumb(self, draw, tail=""):
        txt = "WI-FI" + ("  ›  " + tail if tail else "")
        gauges.tracked_text(draw, 40 * SS, 80 * SS, txt,
                            font(25 * SS, "bold"), TEXT_DIM, tracking=3 * SS)

    # -- list ------------------------------------------------------------- #
    def _render_list(self, draw):
        self._crumb(draw)
        st = wifinet.status()
        self._draw_current(draw, st)
        gauges.tracked_text(draw, AREA_X0 * SS, 217 * SS, "AVAILABLE NETWORKS",
                            font(17 * SS, "bold"), TEXT_DIM, tracking=3 * SS)
        scanning = wifinet.scanning()
        self._btn(draw, HOTSPOT_BTN, "HOTSPOT (CAR)", None, TEXT_DIM,
                  outline=CARD_BORDER)
        self._btn(draw, RESCAN_BTN, "SCANNING…" if scanning else "RESCAN",
                  None, ACCENT_GLOW if scanning else TEXT_DIM,
                  outline=CARD_BORDER)

        nets = wifinet.networks()
        if not nets:
            gauges._centered_text(draw, SW // 2, 430 * SS,
                                  "SCANNING…" if scanning else "NO NETWORKS",
                                  font(30 * SS, "bold"), TEXT_DIM)
        else:
            self._scroll = max(0, min(self._scroll, max(0, len(nets) - N_VIS)))
            shown = nets[self._scroll:self._scroll + N_VIS]
            for i, net in enumerate(shown):
                self._draw_row(draw, ROW_TOP + i * ROW_H, net)
            if len(nets) > N_VIS:
                self._draw_scrollbar(draw, len(nets))
        gauges.tracked_text_center(
            draw, SW // 2, 724 * SS,
            "TAP A NETWORK TO CONNECT      SWIPE = SCROLL      HOLD = BACK",
            font(15 * SS, "bold"), mix(BG, TEXT_DIM, 0.65), tracking=2 * SS)

    def _draw_current(self, draw, st):
        x0, y0, x1, y1 = (v * SS for v in CC)
        gauges.card(draw, x0, y0, x1, y1, radius=12 * SS, scale=SS)
        cy = (CC[1] + CC[3]) // 2
        if st.get("connected"):
            self._bars(draw, 50, CC[3] - 16, st.get("signal") or 0, OK)
            gauges.tracked_text(draw, 116 * SS, (CC[1] + 24) * SS, "CONNECTED",
                                font(15 * SS, "bold"), OK, tracking=2 * SS)
            _vt(draw,116, CC[1] + 52, st.get("ssid") or "--",
                          font(28 * SS, "regular"), TEXT)
            ip = st.get("ip") or "--"
            sec = st.get("security") or ""
            _vt(draw,1232, CC[1] + 28, ip, font(20 * SS, "mono"),
                          TEXT_DIM, right=True)
            sig = st.get("signal")
            meta = ("%d%%" % sig if sig is not None else "--") + (
                "  ·  " + sec if sec else "")
            _vt(draw,1232, CC[1] + 56, meta, font(19 * SS, "regular"),
                          TEXT_DIM, right=True)
        else:
            _vt(draw,116, cy, "NOT CONNECTED",
                          font(26 * SS, "regular"), TEXT_DIM)
            self._bars(draw, 50, CC[3] - 16, 0, TEXT_DIM)

    def _draw_row(self, draw, top, net):
        cy = top + ROW_H // 2
        if top > ROW_TOP:
            draw.line([(48 * SS, top * SS), (AREA_X1 * SS, top * SS)],
                      fill=mix(PANEL, CARD_BORDER, 0.6), width=SS)
        secured = wifinet.is_secured(net.get("security"))
        col = ACCENT_GLOW if net.get("in_use") else (
            TEXT if net.get("saved") else mix(TEXT, BG, 0.05))
        self._bars(draw, 52, cy + 16, net.get("signal") or 0, col)
        _vt(draw,116, cy - 13, net.get("ssid") or "--",
                      font(27 * SS, "regular"), TEXT)
        sub = net.get("security") or "open"
        if net.get("saved"):
            sub += "  ·  saved"
        _vt(draw,116, cy + 16, sub, font(17 * SS, "regular"),
                      TEXT_DIM)
        right = AREA_X1
        if net.get("in_use"):
            right = self._tag(draw, right, cy, "CONNECTED", ACCENT,
                              (255, 255, 255))
        elif net.get("saved"):
            right = self._tag(draw, right, cy, "SAVED", None, TEXT_DIM,
                              outline=CARD_BORDER)
        if secured:
            self._lock(draw, (right - 24), cy, TEXT_DIM)
        else:
            _vt(draw,right - 8, cy, "open",
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

    # -- password --------------------------------------------------------- #
    def _render_password(self, draw):
        self._crumb(draw, "CONNECT")
        ssid = (self._target or {}).get("ssid") or "--"
        self._lock(draw, 46, 156, TEXT_DIM)
        _vt(draw,74, 158, ssid, font(32 * SS, "regular"), TEXT)
        gauges.tracked_text(draw, AREA_X0 * SS, 118 * SS, "JOIN NETWORK",
                            font(15 * SS, "bold"), mix(BG, TEXT_DIM, 0.7),
                            tracking=3 * SS)
        # field
        fx0, fy0, fx1, fy1 = (v * SS for v in PW_FIELD)
        draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=12 * SS,
                               fill=mix(BG, PANEL, 0.8), outline=ACCENT,
                               width=2 * SS)
        pw = self._kb.text
        if self._show_pw:
            _vt(draw,PW_FIELD[0] + 24, (PW_FIELD[1] + PW_FIELD[3]) // 2,
                          pw or "", font(28 * SS, "mono"), TEXT)
        else:
            cyf = (PW_FIELD[1] + PW_FIELD[3]) // 2
            for i in range(len(pw)):
                dx = (PW_FIELD[0] + 30 + i * 26) * SS
                draw.ellipse([dx - 5 * SS, cyf * SS - 5 * SS,
                              dx + 5 * SS, cyf * SS + 5 * SS], fill=mix(TEXT, BG, 0.1))
        # buttons
        self._btn(draw, SHOW_BTN, "HIDE" if self._show_pw else "SHOW", None,
                  TEXT_DIM, outline=CARD_BORDER)
        self._btn(draw, CANCEL_BTN, "CANCEL", None, TEXT_DIM,
                  outline=CARD_BORDER)
        self._btn(draw, CONNECT_BTN, "CONNECT", ACCENT, (255, 255, 255))
        self._kb.render(draw, None)

    # -- connecting / result --------------------------------------------- #
    def _render_connecting(self, draw):
        self._crumb(draw, "CONNECT")
        ssid = wifinet.connect_state().get("ssid") or "--"
        gauges._centered_text(draw, SW // 2, 320 * SS, "CONNECTING",
                              font(50 * SS, "bold"), TEXT)
        gauges._centered_text(draw, SW // 2, 372 * SS, "to %s" % ssid,
                              font(24 * SS, "regular"), TEXT_DIM)
        lit = int(time.monotonic() / 0.4) % 3
        for i in range(3):
            dx = SW // 2 + (i - 1) * 36 * SS
            col = ACCENT_GLOW if i == lit else mix(BG, TEXT_DIM, 0.5)
            r = 7 * SS
            draw.ellipse([dx - r, 430 * SS - r, dx + r, 430 * SS + r], fill=col)

    def _advance_connecting(self):
        cs = wifinet.connect_state()
        elapsed = time.monotonic() - self._connect_t0
        if elapsed >= _CONNECT_VIEW_MIN and cs.get("state") in ("ok", "failed"):
            self._mode = "result"
        elif elapsed >= _CONNECT_TIMEOUT:
            self._mode = "result"   # never strand the user on the spinner

    def _render_result(self, draw):
        self._crumb(draw, "CONNECT")
        cs = wifinet.connect_state()
        ok = cs.get("state") == "ok"
        title, col = ("CONNECTED", OK) if ok else ("CONNECTION FAILED", DANGER)
        x0, x1 = (AREA_X0 + 60) * SS, (AREA_X1 - 60) * SS
        draw.rounded_rectangle([x0, 250 * SS, x1, 470 * SS], radius=16 * SS,
                               fill=mix(BG, col, 0.16),
                               outline=mix(BG, col, 0.6), width=2 * SS)
        gauges._centered_text(draw, SW // 2, 322 * SS, title,
                              font(44 * SS, "bold"), col)
        if ok:
            st = wifinet.status()
            sub = "%s  ·  %s" % (cs.get("ssid") or "--", st.get("ip") or "--")
        else:
            sub = (cs.get("error") or "check the password and try again")[:64]
        gauges._centered_text(draw, SW // 2, 388 * SS, sub,
                              font(22 * SS, "regular"), TEXT_DIM)
        gauges._centered_text(draw, SW // 2, 452 * SS, "TAP TO CONTINUE",
                              font(17 * SS, "bold"), mix(BG, TEXT_DIM, 0.7))

    # -- action sheet ----------------------------------------------------- #
    def _open_sheet(self, net):
        ssid = net.get("ssid")
        name = wifinet.saved_name_for(ssid)
        btns = []
        if net.get("in_use"):
            btns.append(("DISCONNECT", "disconnect"))
        else:
            btns.append(("CONNECT", "connect"))
        if name:
            btns.append(("FORGET", "forget"))
        btns.append(("CANCEL", "cancel"))
        self._sheet = {"ssid": ssid, "name": name, "net": net, "buttons": btns}

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
        gauges.tracked_text_center(draw, cx, 300 * SS, "NETWORK",
                                   font(16 * SS, "bold"), TEXT_DIM, tracking=4 * SS)
        gauges._centered_text(draw, cx, 360 * SS,
                              (self._sheet.get("ssid") or "--")[:24],
                              font(30 * SS, "regular"), TEXT)
        for (label, kind), rect in zip(self._sheet["buttons"],
                                       self._sheet_button_rects()):
            danger = kind == "forget"
            accent = kind in ("connect", "disconnect")
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
        if self._mode == "password":
            return self._tap_password(x, y)
        if self._mode == "result":
            self._mode = "list"
            wifinet.poke()
            return True
        return True  # connecting: swallow

    def _tap_list(self, x, y):
        if self._hit(RESCAN_BTN, x, y):
            wifinet.rescan()
            return True
        if self._hit(HOTSPOT_BTN, x, y):
            if wifinet.hotspot():        # no-op (+ stay on list) if unsaved
                self._begin_connecting()
            return True
        if self._hit(CC, x, y) and wifinet.status().get("connected"):
            st = wifinet.status()
            self._open_sheet({"ssid": st.get("ssid"), "in_use": True})
            return True
        nets = wifinet.networks()
        if ROW_TOP <= y < ROW_TOP + N_VIS * ROW_H and nets:
            i = (y - ROW_TOP) // ROW_H + self._scroll
            if 0 <= i < len(nets):
                self._on_pick(nets[i])
        return True

    def _on_pick(self, net):
        if net.get("in_use") or net.get("saved"):
            self._open_sheet(net)
        elif wifinet.is_secured(net.get("security")):
            self._target = net
            self._show_pw = False
            self._kb.reset()
            self._mode = "password"
        elif wifinet.connect(net.get("ssid")):
            self._begin_connecting()

    def _tap_password(self, x, y):
        if self._hit(SHOW_BTN, x, y):
            self._show_pw = not self._show_pw
            return True
        if self._hit(CANCEL_BTN, x, y):
            self._mode = "list"
            self._kb.reset()
            return True
        if self._hit(CONNECT_BTN, x, y):
            self._do_connect()
            return True
        if self._kb.handle_tap(x, y) == "enter":
            self._do_connect()
        return True

    def _do_connect(self):
        ssid = (self._target or {}).get("ssid")
        if not ssid:
            self._mode = "list"
            return
        started = wifinet.connect(ssid, self._kb.text or None)
        self._kb.reset()
        if started:
            self._begin_connecting()
        else:
            self._mode = "list"

    def _begin_connecting(self):
        self._connect_t0 = time.monotonic()
        self._mode = "connecting"

    def _tap_sheet(self, x, y):
        for (label, kind), rect in zip(self._sheet["buttons"],
                                       self._sheet_button_rects()):
            if self._hit(rect, x, y):
                name = self._sheet.get("name")
                ssid = self._sheet.get("ssid")
                self._sheet = None
                if kind == "connect":
                    started = (wifinet.connect_saved(name, ssid) if name
                               else wifinet.connect(ssid))
                    if started:
                        self._begin_connecting()
                elif kind == "disconnect":
                    wifinet.disconnect()
                    self._mode = "list"
                elif kind == "forget":
                    wifinet.forget(name)
                    self._mode = "list"
                return True
        self._sheet = None  # tap outside a button dismisses
        return True

    def handle_hold(self, x, y, ctx):
        if self._sheet is not None:
            self._sheet = None
            return True
        if self._mode != "list":
            self._mode = "list"
            self._kb.reset()
            return True
        return False  # list + no sheet: let the nav layer exit the page

    def handle_swipe_v(self, direction, ctx):
        if self._mode != "list" or self._sheet is not None:
            return True
        nets = wifinet.networks()
        step = 2 if direction == "up" else -2
        self._scroll = max(0, min(self._scroll + step, max(0, len(nets) - N_VIS)))
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
    def _bars(draw, gx, base_y, signal, color):
        try:
            sig = max(0, min(100, int(signal)))
        except Exception:
            sig = 0
        lit = 1 if sig < 25 else 2 if sig < 50 else 3 if sig < 75 else 4
        heights = (12, 19, 26, 33)
        for i in range(4):
            x = (gx + i * 12) * SS
            h = heights[i] * SS
            col = color if i < lit else mix(BG, theme.TICK, 0.9)
            draw.rectangle([x, base_y * SS - h, x + 7 * SS, base_y * SS], fill=col)

    @staticmethod
    def _lock(draw, cx, cy, col):
        u = SS
        draw.rounded_rectangle([(cx - 9) * u, (cy - 1) * u,
                                (cx + 9) * u, (cy + 13) * u], radius=2 * u,
                               fill=col)
        draw.arc([(cx - 6) * u, (cy - 9) * u, (cx + 6) * u, (cy + 3) * u],
                 180, 360, fill=col, width=max(1, 2 * u))

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
