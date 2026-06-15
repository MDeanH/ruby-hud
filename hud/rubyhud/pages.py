"""Page framework + the three HUD pages (gauges / CAN bus / system).

Each Page renders the BODY of the screen between the global top and bottom
strips drawn by render.compose_frame; coordinates here are screen pixels
multiplied by render.SS (the shared supersample factor).

Static/dynamic split: render_static(draw, img) draws the page's never-
changing chrome (dial face, card chrome, titles, zebra rows, glyphs) ONCE
into the static layer cached by render._page_static; render(draw, img, snap,
ctx) draws only the per-frame dynamic elements on a copy of that layer.

ctx is a plain dict shared across frames: theme/gauges module refs, the CAN
channel name, CanPage's frozen state and the CPU sparkline history. The
sysinfo helpers SystemPage uses are defined here; each one is cached and
failure-guarded so a missing tool or file renders '--' instead of raising.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import deque

from . import gauges, theme
from .render import (COOLANT_HI, COOLANT_LO, RPM_MAX, RPM_REDLINE, SH, SS, SW,
                     VOLTS_HI, VOLTS_LO, _frac, _num)
from .signals import _run
from .theme import (ACCENT, ACCENT_GLOW, CARD_BORDER, CARD_EDGE, DANGER, OK,
                    PANEL, ROW_A, ROW_B, TEXT, TEXT_DIM, TICK, WARN, font, mix)

from . import config

BG = theme.BG


def _disp_temp(c):
    """Celsius -> displayed value in the active unit (see config). Internal
    state + thresholds stay Celsius; this is display-layer only."""
    return config.c_to_disp(c)


def _disp_speed(mph):
    """mph -> displayed value in the active speed unit (see config)."""
    return config.mph_to_disp(mph)


class Page:
    """Base page: render the body; handle_* return True when consumed."""

    name = "PAGE"
    hidden = False   # True => not in the swipe rotation; reached via deep-link

    def render_static(self, draw, img):
        """Draw never-changing page chrome into the static layer (once)."""
        pass

    def render(self, draw, img, snap, ctx):
        pass

    def handle_tap(self, x, y, ctx):
        return False

    def handle_hold(self, x, y, ctx):
        """Long-press; unconsumed holds cycle pages (see __main__)."""
        return False

    def handle_swipe_v(self, direction, ctx):
        """Vertical swipe; direction is 'up' or 'down'."""
        return False

    def on_show(self, ctx):
        """Called once when this page becomes active (page_idx switches to it)."""
        pass

    def on_hide(self, ctx):
        """Called once when this page stops being active (switch away)."""
        pass


# --------------------------------------------------------------------------- #
# Page 1: gauges (the hero screen)
# --------------------------------------------------------------------------- #
class GaugesPage(Page):
    """Tesla-language driving display: a hairline rpm arc with a red redline
    segment wraps the left, a large hero speed numeral sits center-left, a big
    gear on the right, and a calm row of vitals (coolant / fuel / battery /
    throttle) along the bottom-right. Numbers are the design — no skeuomorphic
    dial. Static chrome (arc track + redline + labels + vital tracks) renders
    once; only the live values + arc/dot render per frame."""

    name = "GAUGES"

    # rpm arc (screen px, pre-supersample). 0 rpm at ARC_A0; full range sweeps
    # ARC_SWEEP deg clockwise (0deg = 3 o'clock, PIL convention).
    ARC_CX, ARC_CY, ARC_R = 415, 410, 285
    # Standard tach orientation: opening at the BOTTOM. 0 rpm at lower-left
    # (135deg), sweeping clockwise 270deg up and over to lower-right (redline).
    ARC_A0, ARC_SWEEP = 135.0, 270.0
    REDLINE = 7000.0                      # of RPM_MAX (8000)

    # hero numerals (drawn anchor middle/baseline).
    SPD_CX, SPD_BASE = 430, 430
    GEAR_CX, GEAR_BASE = 980, 470
    STRIP_Y = 558
    GEARS = ("N", "1", "2", "3", "4", "5", "6")

    # bottom-right vitals row (kept clear of the global bottom strip at y>=736).
    VIT_XS = (700, 836, 972, 1108)
    VIT_LABEL_Y, VIT_VALUE_Y, VIT_BAR_Y = 612, 654, 666
    VIT_BAR_W = 112

    def _rpm_a1(self, rpm):
        return self.ARC_A0 + _frac(rpm, 0, RPM_MAX) * self.ARC_SWEEP

    # ---- static chrome -------------------------------------------------- #
    def render_static(self, draw, img):
        cx, cy, r = self.ARC_CX * SS, self.ARC_CY * SS, self.ARC_R * SS
        a0, a1 = self.ARC_A0, self.ARC_A0 + self.ARC_SWEEP
        rl0 = self.ARC_A0 + (self.REDLINE / RPM_MAX) * self.ARC_SWEEP
        # arc track: faint halo + dim base line.
        gauges.arc_glow(img, cx, cy, r, a0, a1, mix(theme.BG, TICK, 0.5),
                        8 * SS, 6 * SS)
        gauges.arc_seg(draw, cx, cy, r, a0, a1, mix(theme.BG, TEXT_DIM, 0.32),
                       3 * SS)
        # redline segment — bright + strong glow (pops on entry).
        gauges.arc_glow(img, cx, cy, r, rl0, a1, DANGER, 8 * SS, 6 * SS)
        gauges.arc_seg(draw, cx, cy, r, rl0, a1, DANGER, 4 * SS)
        # tick labels 0,2,4,6,8 (thousands) just inside the arc.
        tf = font(15 * SS, "regular")
        for i, lab in enumerate(("0", "2", "4", "6", "8")):
            px, py = gauges._polar(cx, cy, (self.ARC_R - 36) * SS,
                                   a0 + (i / 4.0) * self.ARC_SWEEP)
            gauges._centered_text(draw, px, py, lab, tf, mix(theme.BG, TEXT_DIM, 0.55))
        # speed unit + rpm/gear labels.
        gauges.tracked_text_center(draw, self.SPD_CX * SS, (self.SPD_BASE + 56) * SS,
                                   config.speed_label().upper(),
                                   font(22 * SS, "regular"), TEXT_DIM, tracking=9 * SS)
        gauges.tracked_text_center(draw, self.ARC_CX * SS, 636 * SS, "RPM",
                                   font(14 * SS, "regular"),
                                   mix(theme.BG, TEXT_DIM, 0.7), tracking=4 * SS)
        gauges.tracked_text_center(draw, self.GEAR_CX * SS, 300 * SS, "GEAR",
                                   font(14 * SS, "regular"),
                                   mix(theme.BG, TEXT_DIM, 0.7), tracking=6 * SS)
        # vitals: divider + per-vital label + dim track bar.
        draw.line([(700 * SS, 586 * SS), (1232 * SS, 586 * SS)],
                  fill=mix(theme.BG, CARD_BORDER, 0.7), width=max(1, SS))
        lf = font(13 * SS, "regular")
        for x, lab in zip(self.VIT_XS, ("COOLANT", "FUEL", "BATTERY", "THROTTLE")):
            gauges.tracked_text(draw, x * SS, self.VIT_LABEL_Y * SS, lab, lf,
                                mix(theme.BG, TEXT_DIM, 0.6), tracking=3 * SS,
                                anchor="ls")
            draw.rounded_rectangle([x * SS, self.VIT_BAR_Y * SS,
                                    (x + self.VIT_BAR_W) * SS,
                                    (self.VIT_BAR_Y + 3) * SS],
                                   radius=int(1.5 * SS), fill=mix(theme.BG, TICK, 0.85))

    # ---- dynamic -------------------------------------------------------- #
    def render(self, draw, img, snap, ctx):
        self._rpm_arc(draw, img, snap)
        self._speed(draw, snap)
        self._gear(draw, snap)
        self._vitals(draw, snap)
        self._warnings(draw, img, snap)

    def _rpm_arc(self, draw, img, snap):
        cx, cy, r = self.ARC_CX * SS, self.ARC_CY * SS, self.ARC_R * SS
        rpm = _num(snap.rpm)
        in_red = rpm is not None and float(rpm) >= self.REDLINE
        accent = DANGER if in_red else TEXT
        if rpm is not None and float(rpm) > 0:
            a1 = self._rpm_a1(rpm)
            gauges.arc_seg(draw, cx, cy, r, self.ARC_A0, a1, accent, 4 * SS)
            dx, dy = gauges._polar(cx, cy, r, a1)
            g = gauges.glow_dot(int(14 * SS), ACCENT_GLOW if in_red else TEXT,
                                strength=0.7)
            img.paste(g, (int(dx - g.width / 2), int(dy - g.height / 2)), g)
            draw.ellipse([dx - 5 * SS, dy - 5 * SS, dx + 5 * SS, dy + 5 * SS],
                         fill=accent)
        vt = "--" if rpm is None else "%d" % int(round(float(rpm)))
        gauges._centered_text(draw, self.ARC_CX * SS, 608 * SS, vt,
                              font(34 * SS, "thin"), accent if in_red else TEXT)

    def _speed(self, draw, snap):
        speed = _num(snap.speed_mph)
        spd = "--" if speed is None else "%d" % int(round(_disp_speed(float(speed))))
        try:
            draw.text((self.SPD_CX * SS, self.SPD_BASE * SS), spd,
                      font=font(280 * SS, "thin"), fill=TEXT, anchor="ms")
        except Exception:
            gauges._centered_text(draw, self.SPD_CX * SS, (self.SPD_BASE - 90) * SS,
                                  spd, font(280 * SS, "thin"), TEXT)

    def _gear(self, draw, snap):
        g = snap.gear or "-"
        glyph = g if g != "-" else "–"
        try:
            draw.text((self.GEAR_CX * SS, self.GEAR_BASE * SS), glyph,
                      font=font(220 * SS, "thin"), fill=TEXT, anchor="ms")
        except Exception:
            gauges._centered_text(draw, self.GEAR_CX * SS, (self.GEAR_BASE - 70) * SS,
                                  glyph, font(220 * SS, "thin"), TEXT)
        sf = font(17 * SS, "regular")
        step = 36
        x0 = self.GEAR_CX - (len(self.GEARS) - 1) * step // 2
        for i, gl in enumerate(self.GEARS):
            gx = (x0 + i * step) * SS
            cur = (gl == g)
            gauges._centered_text(draw, gx, self.STRIP_Y * SS, gl, sf,
                                  TEXT if cur else mix(theme.BG, TEXT_DIM, 0.45))
            if cur:
                draw.line([(gx - 9 * SS, (self.STRIP_Y + 16) * SS),
                           (gx + 9 * SS, (self.STRIP_Y + 16) * SS)],
                          fill=TEXT, width=max(1, int(2.5 * SS)))

    def _vitals(self, draw, snap):
        coolant, fuel = _num(snap.coolant_c), _num(snap.fuel_pct)
        volts, thr = _num(snap.volts), _num(snap.throttle_pct)
        ccol = TEXT if coolant is None else (
            DANGER if coolant > 110 else WARN if coolant > 100 else TEXT)
        fcol = TEXT if fuel is None else (
            DANGER if fuel < 10 else WARN if fuel < 20 else TEXT)
        vcol = TEXT if volts is None else (
            DANGER if volts < 11.2 else WARN if volts < 11.8 else TEXT)
        items = (
            (None if coolant is None else "%d" % round(_disp_temp(float(coolant))),
             config.temp_label(), _frac(snap.coolant_c, COOLANT_LO, COOLANT_HI), ccol),
            (None if fuel is None else "%d" % round(float(fuel)), "%",
             (float(fuel) / 100.0 if fuel is not None else 0.0), fcol),
            (None if volts is None else "%.1f" % float(volts), "V",
             _frac(snap.volts, VOLTS_LO, VOLTS_HI), vcol),
            (None if thr is None else "%d" % round(float(thr)), "%",
             (float(thr) / 100.0 if thr is not None else 0.0), TEXT),
        )
        vf, uf = font(38 * SS, "thin"), font(19 * SS, "regular")
        for x, (val, unit, frac, col) in zip(self.VIT_XS, items):
            vx, vy = x * SS, self.VIT_VALUE_Y * SS
            s = "--" if val is None else val
            draw.text((vx, vy), s, font=vf, fill=col, anchor="ls")
            try:
                w = draw.textlength(s, font=vf)
            except Exception:
                w = len(s) * 20 * SS
            draw.text((vx + w + 6 * SS, vy), unit, font=uf, fill=TEXT_DIM,
                      anchor="ls")
            fw = int(max(0.0, min(1.0, frac)) * self.VIT_BAR_W * SS)
            if fw > 0:
                barcol = col if col in (WARN, DANGER) else mix(TEXT_DIM, TEXT, 0.25)
                draw.rounded_rectangle([vx, self.VIT_BAR_Y * SS, vx + fw,
                                        (self.VIT_BAR_Y + 3) * SS],
                                       radius=int(1.5 * SS), fill=barcol)

    @staticmethod
    def _warnings(draw, img, snap):
        if not snap.warnings:
            return
        bw = 720 * SS
        bx = SW // 2 - bw // 2
        by = 656 * SS
        phase = time.monotonic() % 1.0
        gauges.warning_banner(img, draw, bx, by, bw, list(snap.warnings),
                              phase, scale=SS)


# --------------------------------------------------------------------------- #
# Page 2: vehicle dashboard (every decoded ND1 RF signal at a glance)
# --------------------------------------------------------------------------- #
class VehiclePage(Page):
    """Auto-surfaces the full live signal set decoded in signals._decode_mx5
    for the 2017 ND1 MX-5 GT RF: a tile grid of numeric signals plus a chip
    strip for the boolean/enum signals (RF roof, headlights, turn, parking
    brake, reverse). Card chrome + titles are drawn once into the static
    layer; only values/chips render per frame. Every value is guarded so a
    missing/stale signal shows '--' rather than raising."""

    name = "VEHICLE"

    # 4 columns x 2 rows of vital atoms (screen px, pre-supersample).
    COL_XS = (56, 360, 664, 968)
    ROW_YS = (172, 408)            # label baselines
    VAL_SIZE, BAR_W = 64, 200

    # Bottom status-chip strip.
    CHIP_Y = 548
    CHIP_X = 56

    def _slots(self):
        return [(x, y) for y in self.ROW_YS for x in self.COL_XS]

    # ---- static chrome ---------------------------------------------------
    def render_static(self, draw, img):
        gauges.tracked_text(draw, 56 * SS, 92 * SS, "VEHICLE",
                            font(22 * SS, "regular"), TEXT_DIM, tracking=8 * SS)
        draw.line([(self.CHIP_X * SS, (self.CHIP_Y - 18) * SS),
                   ((SW // SS - self.CHIP_X) * SS, (self.CHIP_Y - 18) * SS)],
                  fill=mix(theme.BG, CARD_BORDER, 0.7), width=max(1, SS))

    # ---- dynamic ---------------------------------------------------------
    def render(self, draw, img, snap, ctx):
        for (x, ly), atom in zip(self._slots(), self._atoms(snap)):
            label, value, unit, frac, color, sub = atom
            gauges.vital_atom(draw, x * SS, ly * SS, label, value, unit, color,
                              frac=frac, sub=sub, scale=SS,
                              value_size=self.VAL_SIZE, bar_w=self.BAR_W)
        self._chip_strip(draw, snap)

    @staticmethod
    def _atoms(snap):
        """Per-atom (label, value, unit, frac|None, color, sub|None), row-major
        matching _slots()."""
        sp, rpm = _num(snap.speed_mph), _num(snap.rpm)
        g = snap.gear or "-"
        cool, fuel = _num(snap.coolant_c), _num(snap.fuel_pct)
        thr, mp, soc = _num(snap.throttle_pct), _num(snap.map_kpa), _num(snap.cpu_temp_c)
        # Calm by default: white numerals; colour only on warning/alert (matches
        # the GAUGES page — green/amber/red are reserved for state, not steady).
        rpm_col = DANGER if (rpm is not None and rpm >= 7000) else TEXT
        cool_col = (TEXT if cool is None else
                    DANGER if cool > 110 else WARN if cool > 100 else TEXT)
        fuel_col = (TEXT if fuel is None else
                    DANGER if fuel < 10 else WARN if fuel < 20 else TEXT)
        soc_col = (TEXT if soc is None else
                   DANGER if soc > 80 else WARN if soc > 70 else TEXT)
        return [
            ("SPEED", None if sp is None else "%d" % round(_disp_speed(sp)),
             config.speed_label(), None, TEXT, None),
            ("RPM", None if rpm is None else "%d" % round(rpm), "",
             None, rpm_col, None),
            ("GEAR", g if g != "-" else None, "", None, TEXT, None),
            ("COOLANT", None if cool is None else "%d" % round(_disp_temp(cool)),
             config.temp_label(), _frac(snap.coolant_c, COOLANT_LO, COOLANT_HI),
             cool_col, None),
            ("FUEL", None if fuel is None else "%d" % round(fuel), "%",
             (fuel / 100.0 if fuel is not None else None), fuel_col, None),
            ("THROTTLE", None if thr is None else "%d" % round(thr), "%",
             (thr / 100.0 if thr is not None else None), TEXT, None),
            ("MAP", None if mp is None else "%d" % round(mp), "kPa",
             None, TEXT, None if mp is None else "%+.0f vs baro" % (mp - 101.3)),
            ("SoC", None if soc is None else "%d" % round(_disp_temp(soc)),
             config.temp_label(),
             (_frac(soc, 0, 100) if soc is not None else None), soc_col, None),
        ]

    def _chip_strip(self, draw, snap):
        x = self.CHIP_X * SS
        y = self.CHIP_Y * SS
        roof = snap.roof or "-"
        # RF hardtop: green when closed, amber otherwise (open / in transit /
        # uncalibrated code) so a non-closed top is always conspicuous.
        roof_col = OK if roof == "CLOSED" else (TEXT_DIM if roof == "-" else WARN)
        turn = snap.turn or "off"
        chips = [
            ("ROOF " + roof, roof_col, roof == "CLOSED"),
            ("L", OK if turn in ("L", "LR") else TEXT_DIM,
             turn in ("L", "LR")),
            ("R", OK if turn in ("R", "LR") else TEXT_DIM,
             turn in ("R", "LR")),
            ("LIGHTS " + (snap.headlight or "off").upper(),
             ACCENT if (snap.headlight or "off") != "off" else TEXT_DIM,
             (snap.headlight or "off") != "off"),
            ("P-BRAKE", WARN if snap.parking_brake else TEXT_DIM,
             bool(snap.parking_brake)),
            ("REVERSE", WARN if snap.reverse else TEXT_DIM,
             bool(snap.reverse)),
            ("SRC " + (snap.source or "?"),
             OK if snap.source == "LIVE" else TEXT_DIM, False),
        ]
        for txt, col, filled in chips:
            w, _ = gauges.status_chip(draw, x, y, txt, col, filled=filled,
                                      scale=SS)
            x += w + 12 * SS


# --------------------------------------------------------------------------- #
# Page: raw CAN traffic (hidden — reached via CONFIGURE > CAN BUS)
# --------------------------------------------------------------------------- #
class CanPage(Page):
    name = "CAN BUS"
    hidden = True

    # Frame-list hit box / panel (screen px, pre-supersample). Left ~60%.
    LIST_X0, LIST_X1 = 32, 760
    LIST_Y0, LIST_Y1 = 130, 724
    ROW_H, N_ROWS = 38, 14
    ROWS_TOP = 174  # LIST_Y0 + 44

    # Right id-table panel. Rows start below the column-header underline;
    # rate bars sit on the text line (vertically centered via BAR_DY).
    TBL_X0, TBL_X1 = 776, 1248
    TBL_ROW0, TBL_ROW_H, TBL_N = 226, 56, 8
    BAR_X0, BAR_X1, BAR_H, BAR_DY = 1000, 1170, 10, 9

    def render_static(self, draw, img):
        x0, y0 = self.LIST_X0 * SS, self.LIST_Y0 * SS
        x1, y1 = self.LIST_X1 * SS, self.LIST_Y1 * SS

        # Header band card above the panels.
        gauges.card(draw, x0, 82 * SS, self.TBL_X1 * SS, 122 * SS,
                    radius=10 * SS, scale=SS)

        # Frame list card + zebra rows + column header.
        gauges.card(draw, x0, y0, x1, y1, radius=14 * SS, scale=SS)
        for i in range(self.N_ROWS):
            ry = (self.ROWS_TOP + i * self.ROW_H) * SS
            draw.rectangle([x0 + 8 * SS, ry, x1 - 8 * SS,
                            ry + self.ROW_H * SS - SS],
                           fill=ROW_A if i % 2 == 0 else ROW_B)
        draw.text((x0 + 20 * SS, y0 + 12 * SS),
                  "%-8s %2s  %-23s %7s" % ("ID", "L", "DATA", "AGE"),
                  font=font(19 * SS, "mono"), fill=TEXT_DIM)
        draw.line([(x0 + 12 * SS, y0 + 40 * SS), (x1 - 12 * SS,
                                                  y0 + 40 * SS)],
                  fill=CARD_BORDER, width=SS)

        # Id table card + column header + static rate-bar tracks.
        tx0, tx1 = self.TBL_X0 * SS, self.TBL_X1 * SS
        gauges.card(draw, tx0, y0, tx1, y1, radius=14 * SS, scale=SS)
        gauges.tracked_text(draw, tx0 + 20 * SS, y0 + 14 * SS,
                            "TOP IDS BY RATE", font(17 * SS, "bold"),
                            TEXT_DIM, tracking=3 * SS)
        draw.text((tx0 + 20 * SS, y0 + 52 * SS),
                  "%-6s %8s" % ("ID", "Hz"), font=font(20 * SS, "mono"),
                  fill=TEXT_DIM)
        try:
            cw = draw.textlength("COUNT", font=font(20 * SS, "mono"))
        except Exception:
            cw = 5 * 12 * SS
        draw.text((tx1 - 20 * SS - cw, y0 + 52 * SS), "COUNT",
                  font=font(20 * SS, "mono"), fill=TEXT_DIM)
        draw.line([(tx0 + 12 * SS, y0 + 86 * SS),
                   (tx1 - 12 * SS, y0 + 86 * SS)],
                  fill=CARD_BORDER, width=SS)
        # Rate-bar tracks are drawn dynamically, only under populated rows
        # (static tracks for empty slots read as broken UI).

    def render(self, draw, img, snap, ctx):
        now = time.monotonic()
        frames = snap.recent_frames or []
        stats = snap.id_stats or {}
        total = int(snap.total_frames or 0)
        if ctx.get("frozen"):
            view = ctx.get("frozen_view")
            if view is None:
                view = (list(frames), dict(stats), total, now)
                ctx["frozen_view"] = view
            frames, stats, total, now = view

        hdr = "BUS %s   CH %s   FRAMES %d" % (
            snap.can_bus_state or "?", ctx.get("channel", "?"), total)
        draw.text((52 * SS, 92 * SS), hdr, font=font(22 * SS, "bold"),
                  fill=TEXT_DIM)
        if ctx.get("frozen"):
            pw, _ = gauges.status_chip(draw, 0, -999, "PAUSED", WARN,
                                       scale=SS)
            gauges.status_chip(draw, self.LIST_X1 * SS - pw - 8 * SS,
                               86 * SS, "PAUSED", WARN, filled=True,
                               scale=SS)

        self._frame_list(draw, frames, now)
        self._id_table(draw, stats, now)

    def handle_tap(self, x, y, ctx):
        if (self.LIST_X0 <= x <= self.LIST_X1
                and self.LIST_Y0 <= y <= self.LIST_Y1):
            frozen = not ctx.get("frozen", False)
            ctx["frozen"] = frozen
            if not frozen:
                ctx.pop("frozen_view", None)
            return True
        return False

    def _frame_list(self, draw, frames, now):
        x0, y0 = self.LIST_X0 * SS, self.LIST_Y0 * SS
        x1, y1 = self.LIST_X1 * SS, self.LIST_Y1 * SS
        rows = list(frames)[-self.N_ROWS:]
        rows.reverse()  # newest at top
        if not rows:
            gauges._centered_text(draw, (x0 + x1) // 2, (y0 + y1) // 2,
                                  "NO FRAMES", font(28 * SS, "bold"),
                                  TEXT_DIM)
            return
        rfont = font(19 * SS, "mono")
        try:
            ch_w = draw.textlength("0", font=rfont)
        except Exception:
            ch_w = gauges._text_size(draw, "0", rfont)[0]
        ry = self.ROWS_TOP * SS + 4 * SS
        for ts, cid, data in rows:
            try:
                d = bytes(data)[:8]
                age = max(0, int((now - float(ts)) * 1000.0))
                hexs = " ".join("%02X" % b for b in d)
                ids = "%03X" % int(cid)
                rest = "%2d  %-23s %7s" % (len(d), hexs,
                                           "%dms" % min(age, 99999))
            except Exception:
                ids, rest, age = "?", "", 0
            # Recently-active IDs tinted toward the accent glow.
            icol = mix(TEXT, ACCENT_GLOW, 0.65) if age < 100 else TEXT
            draw.text((x0 + 20 * SS, ry), ids, font=rfont, fill=icol)
            draw.text((x0 + 20 * SS + ch_w * 9, ry), rest, font=rfont,
                      fill=TEXT)
            ry += self.ROW_H * SS

    def _id_table(self, draw, stats, now):
        tx0, tx1 = self.TBL_X0 * SS, self.TBL_X1 * SS
        y0, y1 = self.LIST_Y0 * SS, self.LIST_Y1 * SS
        try:
            top = sorted(stats.items(), key=lambda kv: float(kv[1][1]),
                         reverse=True)[:self.TBL_N]
        except Exception:
            top = []
        if not top:
            gauges._centered_text(draw, (tx0 + tx1) // 2, (y0 + y1) // 2,
                                  "NO IDS", font(28 * SS, "bold"), TEXT_DIM)
            return
        rfont = font(20 * SS, "mono")
        # Log-ish scale: 200 Hz pegs the bar.
        import math
        full = math.log10(1.0 + 200.0)
        for i, (cid, st) in enumerate(top):
            try:
                hz = float(st[1])
                ids = "%03X" % int(cid)
                cnt = "%d" % int(st[0])
            except Exception:
                continue
            ty = (self.TBL_ROW0 + i * self.TBL_ROW_H) * SS
            draw.text((tx0 + 20 * SS, ty), "%-6s %8.1f" % (ids, hz),
                      font=rfont, fill=TEXT)
            try:
                cw = draw.textlength(cnt, font=rfont)
            except Exception:
                cw = gauges._text_size(draw, cnt, rfont)[0]
            draw.text((tx1 - 20 * SS - cw, ty), cnt, font=rfont,
                      fill=TEXT_DIM)
            # Mini rate bar (log-ish scale): dim track under this row only,
            # then the accent fill.
            try:
                f = math.log10(1.0 + max(0.0, hz)) / full
            except Exception:
                f = 0.0
            f = max(0.0, min(1.0, f))
            by = (self.TBL_ROW0 + i * self.TBL_ROW_H + self.BAR_DY) * SS
            draw.rounded_rectangle(
                [self.BAR_X0 * SS, by, self.BAR_X1 * SS,
                 by + self.BAR_H * SS],
                radius=self.BAR_H * SS // 2, fill=mix(BG, TICK, 0.4))
            bw = int((self.BAR_X1 - self.BAR_X0) * SS * f)
            if bw > 2 * SS:
                draw.rounded_rectangle(
                    [self.BAR_X0 * SS, by, self.BAR_X0 * SS + bw,
                     by + self.BAR_H * SS],
                    radius=self.BAR_H * SS // 2, fill=ACCENT)


# --------------------------------------------------------------------------- #
# sysinfo cache (used by SystemPage; every getter is guarded + cached)
# --------------------------------------------------------------------------- #
_cache: dict = {}


def _cached(key, ttl, fn):
    """Memoize fn() for ttl seconds; a failure caches as None (no hammering)."""
    now = time.monotonic()
    ent = _cache.get(key)
    if ent is not None and now - ent[0] < ttl:
        return ent[1]
    try:
        val = fn()
    except Exception:
        val = None
    _cache[key] = (now, val)
    return val


_cpu_prev = [None]  # previous (idle, total) jiffies from /proc/stat


def _cpu_percent():
    with open("/proc/stat") as fh:
        parts = fh.readline().split()
    vals = [int(v) for v in parts[1:9]]
    idle = vals[3] + vals[4]
    total = sum(vals)
    prev, _cpu_prev[0] = _cpu_prev[0], (idle, total)
    if prev is None or total <= prev[1]:
        return None
    dt = total - prev[1]
    return max(0.0, min(100.0, 100.0 * (dt - (idle - prev[0])) / dt))


def get_cpu():
    """(busy_pct or None, '0.42 0.31 0.18' or None)."""
    pct = _cached("cpu_pct", 2.0, _cpu_percent)
    load = _cached("load", 2.0, lambda: "%.2f %.2f %.2f" % os.getloadavg())
    return pct, load


def get_temp_c():
    def read():
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return int(fh.read().strip()) / 1000.0
    return _cached("temp", 5.0, read)


def get_mem():
    """(used_gb, total_gb) or None."""
    def read():
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = rest
        tot = float(info["MemTotal"].split()[0]) / 1048576.0
        avail = float(info["MemAvailable"].split()[0]) / 1048576.0
        return (tot - avail, tot)
    return _cached("mem", 5.0, read)


def get_disk():
    """(used_gb, total_gb, used_pct) or None."""
    def read():
        usage = shutil.disk_usage("/")
        gb = 1024.0 ** 3
        return (usage.used / gb, usage.total / gb,
                100.0 * usage.used / max(1, usage.total))
    return _cached("disk", 5.0, read)


def get_net():
    """(wifi ssid or None, 'ip1 ip2' or None)."""
    def ssid():
        out = _run(["iwgetid", "-r"], timeout=2.0)
        return (out or "").strip() or None

    def ips():
        out = _run(["hostname", "-I"], timeout=2.0)
        return " ".join((out or "").split()[:2]) or None
    return _cached("ssid", 10.0, ssid), _cached("ips", 10.0, ips)


def get_ext5v():
    """EXT5V rail volts (float) or None, via vcgencmd; cached 5s."""
    def read():
        out = _run(["vcgencmd", "pmic_read_adc", "EXT5V_V"], timeout=2.0)
        if not out or "=" not in out:
            return None
        return float(out.split("=", 1)[1].strip().rstrip("V"))
    return _cached("ext5v", 5.0, read)


def get_throttled():
    """vcgencmd get_throttled bitmask int or None; cached 5s."""
    def read():
        out = _run(["vcgencmd", "get_throttled"], timeout=2.0)
        if not out or "=" not in out:
            return None
        return int(out.split("=", 1)[1].strip(), 16)
    return _cached("throttled", 5.0, read)


# --------------------------------------------------------------------------- #
# Page 3: system stats
# --------------------------------------------------------------------------- #
_GLYPH = mix(BG, TEXT_DIM, 0.8)


def _glyph_cpu(draw, cx, cy, s):
    """Square chip with pins."""
    w = max(2, int(s * 0.09))
    half = s * 0.34
    draw.rectangle([cx - half, cy - half, cx + half, cy + half],
                   outline=_GLYPH, width=w)
    q = s * 0.13
    draw.rectangle([cx - q, cy - q, cx + q, cy + q], outline=_GLYPH,
                   width=w)
    pin = s * 0.16
    for i in (-1, 0, 1):
        o = i * s * 0.22
        draw.line([(cx + o, cy - half - pin), (cx + o, cy - half)],
                  fill=_GLYPH, width=w)
        draw.line([(cx + o, cy + half), (cx + o, cy + half + pin)],
                  fill=_GLYPH, width=w)
        draw.line([(cx - half - pin, cy + o), (cx - half, cy + o)],
                  fill=_GLYPH, width=w)
        draw.line([(cx + half, cy + o), (cx + half + pin, cy + o)],
                  fill=_GLYPH, width=w)


def _glyph_temp(draw, cx, cy, s):
    """Thermometer: outlined stem, filled bulb, mercury line."""
    w = max(2, int(s * 0.09))
    stem_w = int(s * 0.16)
    top = cy - s * 0.52
    bulb_r = int(s * 0.26)
    bulb_cy = cy + s * 0.30
    draw.rounded_rectangle([cx - stem_w, top, cx + stem_w,
                            bulb_cy - bulb_r],
                           radius=stem_w, outline=_GLYPH, width=w)
    draw.ellipse([cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r,
                  bulb_cy + bulb_r], fill=_GLYPH)
    draw.line([(cx, cy - s * 0.18), (cx, bulb_cy)], fill=_GLYPH,
              width=max(2, int(s * 0.12)))


def _glyph_mem(draw, cx, cy, s):
    """RAM stick: body, notches, contact pins."""
    w = max(2, int(s * 0.09))
    hw, hh = s * 0.5, s * 0.26
    draw.rectangle([cx - hw, cy - hh, cx + hw, cy + hh], outline=_GLYPH,
                   width=w)
    for i in range(4):
        o = -hw + s * 0.2 + i * s * 0.27
        draw.line([(cx + o, cy - hh + s * 0.1), (cx + o, cy + hh - s * 0.1)],
                  fill=_GLYPH, width=w)
    for i in range(6):
        o = -hw + s * 0.08 + i * s * 0.17
        draw.line([(cx + o, cy + hh), (cx + o, cy + hh + s * 0.12)],
                  fill=_GLYPH, width=w)


def _glyph_disk(draw, cx, cy, s):
    """Cylinder."""
    w = max(2, int(s * 0.09))
    hw = s * 0.42
    eh = s * 0.16
    top, bot = cy - s * 0.34, cy + s * 0.34
    draw.ellipse([cx - hw, top - eh, cx + hw, top + eh], outline=_GLYPH,
                 width=w)
    draw.line([(cx - hw, top), (cx - hw, bot)], fill=_GLYPH, width=w)
    draw.line([(cx + hw, top), (cx + hw, bot)], fill=_GLYPH, width=w)
    draw.arc([cx - hw, bot - eh, cx + hw, bot + eh], 0, 180, fill=_GLYPH,
             width=w)


def _glyph_wifi(draw, cx, cy, s):
    """Wifi arcs + dot."""
    w = max(2, int(s * 0.09))
    base = cy + s * 0.34
    for r in (s * 0.55, s * 0.36, s * 0.18):
        draw.arc([cx - r, base - r, cx + r, base + r], 225, 315,
                 fill=_GLYPH, width=w)
    d = max(2, int(s * 0.07))
    draw.ellipse([cx - d, base - d, cx + d, base + d], fill=_GLYPH)


def _glyph_bolt(draw, cx, cy, s):
    """Lightning bolt (filled)."""
    pts = [
        (cx + s * 0.16, cy - s * 0.52), (cx - s * 0.30, cy + s * 0.10),
        (cx - s * 0.04, cy + s * 0.10), (cx - s * 0.16, cy + s * 0.52),
        (cx + s * 0.30, cy - s * 0.12), (cx + s * 0.04, cy - s * 0.12),
    ]
    draw.polygon(pts, fill=_GLYPH)


class SystemPage(Page):
    name = "SYSTEM"

    # 2 columns x 3 rows of vital atoms (screen px, pre-supersample).
    COL_XS = (56, 664)
    ROW_YS = (158, 352, 546)        # label baselines
    VAL_SIZE, BAR_W = 50, 240

    # CPU sparkline (top-left atom, screen px).
    SPARK_X0, SPARK_X1 = 360, 600
    SPARK_Y0, SPARK_Y1 = 176, 232

    def _slots(self):
        return [(x, y) for y in self.ROW_YS for x in self.COL_XS]

    def render_static(self, draw, img):
        gauges.tracked_text(draw, 56 * SS, 92 * SS, "SYSTEM",
                            font(22 * SS, "regular"), TEXT_DIM, tracking=8 * SS)
        draw.line([(self.SPARK_X0 * SS, self.SPARK_Y1 * SS),
                   (self.SPARK_X1 * SS, self.SPARK_Y1 * SS)],
                  fill=mix(theme.BG, TICK, 0.8), width=max(1, SS))

    def render(self, draw, img, snap, ctx):
        s = self._slots()

        # CPU + sparkline.
        cpu_pct, load = get_cpu()
        hist = ctx.get("cpu_hist")
        if hist is None:
            hist = deque(maxlen=60)
            ctx["cpu_hist"] = hist
        if cpu_pct is not None:
            hist.append(float(cpu_pct))
        self._atom(draw, s[0], "CPU",
                   None if cpu_pct is None else "%d" % round(cpu_pct), "%", TEXT,
                   frac=(cpu_pct / 100.0 if cpu_pct is not None else None),
                   sub=None if load is None else "load " + load)
        self._sparkline(draw, hist)

        # TEMP.
        temp = get_temp_c()
        tcol = (TEXT if temp is None else
                DANGER if temp > 80 else WARN if temp > 70 else TEXT)
        self._atom(draw, s[1], "TEMP",
                   None if temp is None else "%d" % round(_disp_temp(temp)),
                   config.temp_label(), tcol,
                   frac=(temp / 100.0 if temp is not None else None))

        # MEMORY.
        mem = get_mem()
        self._atom(draw, s[2], "MEMORY", None if mem is None else "%.1f" % mem[0],
                   "GB", TEXT, frac=(mem[0] / mem[1] if mem and mem[1] else None),
                   sub=None if mem is None else "of %.1f GB" % mem[1])

        # DISK.
        disk = get_disk()
        self._atom(draw, s[3], "DISK /",
                   None if disk is None else "%d" % round(disk[2]), "%", TEXT,
                   frac=(disk[2] / 100.0 if disk is not None else None),
                   sub=None if disk is None else "%.0f / %.0f GB" % (disk[0], disk[1]))

        # NETWORK.
        ssid, ips = get_net()
        self._atom(draw, s[4], "NETWORK", (ssid or "--")[:14], "", TEXT,
                   sub="TS %s   %s" % (snap.tailscale or "?", ips or "--"))

        # POWER (ext 5V rail + UPS, best-effort; badges carry AC / battery / state).
        volts = get_ext5v()
        pcol = (TEXT if volts is None else
                DANGER if volts < 4.8 else WARN if volts < 4.95 else TEXT)
        ups = ctx.get("ups")
        if ups is None:
            ups = UpsClient()
            ctx["ups"] = ups
        ust = ups.status()
        badges = list(self._power_badges())
        if not ups.offline(ust):
            ac, bp = ust.get("ac_present"), ust.get("battery_pct")
            if ac is True:
                badges.append(("AC", OK))
            elif ac is False:
                badges.append(("BAT", WARN))
            if bp is not None:
                try:
                    badges.append(("%d%%" % int(round(float(bp))), TEXT_DIM))
                except Exception:
                    pass
        self._atom(draw, s[5], "POWER EXT5V",
                   None if volts is None else "%.2f" % volts, "V", pcol)
        self._power_chiprow(draw, s[5], badges)

    def _atom(self, draw, slot, label, value, unit, color, frac=None, sub=None):
        gauges.vital_atom(draw, slot[0] * SS, slot[1] * SS, label, value, unit,
                          color, frac=frac, sub=sub, scale=SS,
                          value_size=self.VAL_SIZE, bar_w=self.BAR_W)

    def _sparkline(self, draw, hist):
        """Last 60 CPU samples as a cheap accent polyline (top-left atom)."""
        if not hist or len(hist) < 2:
            return
        x = self.SPARK_X0 * SS
        y0, y1 = self.SPARK_Y0 * SS, self.SPARK_Y1 * SS
        w = (self.SPARK_X1 - self.SPARK_X0) * SS
        n = hist.maxlen or 60
        step = w / float(max(1, n - 1))
        pts = []
        for i, v in enumerate(hist):
            v = max(0.0, min(100.0, float(v)))
            pts.append((x + i * step, y1 - (y1 - y0) * v / 100.0))
        draw.line(pts, fill=ACCENT, width=2 * SS)
        px, py = pts[-1]
        r = 4 * SS
        draw.ellipse([px - r, py - r, px + r, py + r], fill=ACCENT_GLOW)

    def _power_chiprow(self, draw, slot, badges):
        bx = slot[0] * SS
        by = (slot[1] + 98) * SS
        for txt, col in (badges or []):
            bw, _ = gauges.status_chip(draw, bx, by, txt, col, scale=SS)
            bx += bw + 12 * SS

    @staticmethod
    def _power_badges():
        bits = get_throttled()
        if bits is None:
            return [("--", TEXT_DIM)]
        out = []
        if bits & (1 << 0):
            out.append(("UV NOW", DANGER))
        if bits & (1 << 2):
            out.append(("THROTTLED", WARN))
        if bits & (1 << 16):
            out.append(("UV PAST", WARN))
        if not out:
            out.append(("OK", OK))
        return out


# --------------------------------------------------------------------------- #
# Page 4: AI vision (rubyvision service over /dev/shm)
# --------------------------------------------------------------------------- #
# The rubyvision service (separate process / systemd unit) does camera ->
# Hailo inference -> annotated frame and drops files on a tmpfs dir. The HUD
# only reads the latest values; it never blocks on the service, and renders a
# clear OFFLINE / DEMO state when the service is down, stale, or running
# without a camera / NPU. IPC files (see vision/ spec):
#   status.json  written >= 2 Hz: {"v","ts","seq","state","mode","source",
#                "model","inference_fps","pipeline_fps","hailo_temp_c",
#                "soc_temp_c","frame":{"file","w","h","seq"},"detections":[...]}
#   frame.jpg    800x450 RGB JPEG, annotated (boxes/labels + DEMO badge).
#   cmd.json     HUD -> service: {"seq","cmd":"cycle_source|cycle_model"}.
_VISION_DIR_DEFAULT = "/dev/shm/rubyvision"
_VISION_STALE_S = 2.0  # status older than this -> service considered offline


class VisionClient:
    """Read-only-ish view of the rubyvision tmpfs drop dir.

    status() is cached on the status.json mtime so we only re-parse when the
    service rewrites it. frame() decodes the JPEG only when the published
    frame.seq changes, caching the PIL Image otherwise (decode is the only
    non-trivial cost per frame). write_cmd() bumps a local seq and atomically
    writes cmd.json. Everything is failure-guarded: a missing dir / partial
    write / bad JSON degrades to offline rather than raising into render."""

    def __init__(self, vision_dir=None):
        self.dir = vision_dir or os.environ.get(
            "RUBYVISION_DIR", _VISION_DIR_DEFAULT)
        self._status_path = os.path.join(self.dir, "status.json")
        self._cmd_path = os.path.join(self.dir, "cmd.json")
        self._status_mtime = None
        self._status = None         # last parsed status dict
        self._frame_seq = None      # seq of the cached decoded frame
        self._frame_mtime = None    # mtime fallback gate when seq is missing
        self._frame_img = None      # cached PIL.Image (RGB) or None
        self._cmd_seq = 0

    # -- status ------------------------------------------------------------
    def status(self):
        """Latest parsed status dict, or None if missing/unreadable.

        Re-parses only when status.json mtime advances; otherwise returns the
        cached dict (so calling this every frame is cheap)."""
        try:
            mtime = os.path.getmtime(self._status_path)
        except Exception:
            self._status_mtime = None
            self._status = None
            return None
        if mtime != self._status_mtime:
            try:
                with open(self._status_path, "rb") as fh:
                    self._status = json.loads(fh.read().decode("utf-8"))
                # Only commit the mtime AFTER a successful parse, so a malformed
                # but stable file is retried on the next call (rather than being
                # ignored until the next mtime change). Partial mid-write reads
                # are still safe: keep the last good status, never raise.
                self._status_mtime = mtime
            except Exception:
                pass
        return self._status

    def offline(self, status=None):
        """True when there is no status, it is stale (> _VISION_STALE_S), or
        it explicitly reports an error condition we should surface as OFFLINE
        rather than a live preview."""
        st = status if status is not None else self.status()
        if not isinstance(st, dict):
            return True
        # A hard service error must surface as the prominent OFFLINE card even
        # with a fresh timestamp (e.g. camera/HEF open failed, or the clean
        # shutdown status); a small chip would under-surface it.
        if str(st.get("state") or "") == "error":
            return True
        try:
            ts = float(st.get("ts"))
        except Exception:
            return True
        return (time.time() - ts) > _VISION_STALE_S

    # -- frame -------------------------------------------------------------
    def frame(self, status=None):
        """Decoded latest frame as a PIL RGB Image, or None.

        Only decodes when the published frame.seq changes; otherwise returns
        the cached image. The file is read fully into memory first so a
        concurrent service rewrite (atomic os.replace) can't tear the decode."""
        st = status if status is not None else self.status()
        if not isinstance(st, dict):
            return self._frame_img
        finfo = st.get("frame") or {}
        try:
            seq = int(finfo.get("seq"))
        except Exception:
            try:
                seq = int(st.get("seq"))
            except Exception:
                seq = None
        if seq is not None and seq == self._frame_seq and \
                self._frame_img is not None:
            return self._frame_img
        fname = finfo.get("file") or "frame.jpg"
        fpath = os.path.join(self.dir, str(fname))
        # When seq is unresolved (spec guarantees it, so defensive only), gate
        # on the frame file mtime instead of decoding every frame -- a missing
        # seq must still avoid per-frame JPEG decode, not silently defeat the
        # frame-budget guarantee.
        if seq is None and self._frame_img is not None:
            try:
                fmtime = os.path.getmtime(fpath)
            except Exception:
                fmtime = None
            if fmtime is not None and fmtime == self._frame_mtime:
                return self._frame_img
        try:
            from PIL import Image  # lazy: render env always has Pillow
            with open(fpath, "rb") as fh:
                data = fh.read()
            import io
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode != "RGB":
                img = img.convert("RGB")
            self._frame_img = img
            self._frame_seq = seq
            try:
                self._frame_mtime = os.path.getmtime(fpath)
            except Exception:
                self._frame_mtime = None
        except Exception:
            # Keep last good frame; don't blank the preview on one bad read.
            pass
        return self._frame_img

    # -- command -----------------------------------------------------------
    def write_cmd(self, cmd):
        """Atomically write cmd.json asking the service to do `cmd`.

        Bumps a local monotonically-increasing seq so the service can dedupe.
        Never raises (a failed control write must not break the page)."""
        self._cmd_seq += 1
        payload = {"seq": self._cmd_seq, "cmd": str(cmd)}
        tmp = self._cmd_path + ".tmp"
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(json.dumps(payload).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._cmd_path)
            return True
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False


# --------------------------------------------------------------------------- #
# UpsClient — mirrors VisionClient pattern for /dev/shm/rubyups/status.json
# --------------------------------------------------------------------------- #
# The rubyups daemon (when present) writes a best-effort status drop each poll.
# HUD consumes it read-only with mtime gate + full guard so a missing UPS or
# malformed drop never raises into the SYSTEM render. See ups/README.md for the
# payload schema and the conservative state machine (the daemon itself ships
# disabled + dry_run; this is currently read-only telemetry surface).
_UPS_DIR_DEFAULT = "/dev/shm/rubyups"
_UPS_STALE_S = 5.0


class UpsClient:
    """Read-only view of the rubyups tmpfs status drop.

    status() cached on mtime. offline() returns True for missing/stale/NO_HAT.
    Everything is failure-guarded; the SYSTEM page must remain renderable even
    if the UPS HAT is unplugged or the daemon is not running.
    """

    def __init__(self, ups_dir=None):
        self.dir = ups_dir or os.environ.get("RUBYUPS_DIR", _UPS_DIR_DEFAULT)
        self._path = os.path.join(self.dir, "status.json")
        self._mtime = None
        self._status = None

    def status(self):
        try:
            mtime = os.path.getmtime(self._path)
        except Exception:
            self._mtime = None
            self._status = None
            return None
        if mtime != self._mtime:
            try:
                with open(self._path, "rb") as fh:
                    self._status = json.loads(fh.read().decode("utf-8"))
                self._mtime = mtime
            except Exception:
                pass  # keep previous good snapshot
        return self._status

    def offline(self, st=None):
        st = st if st is not None else self.status()
        if not isinstance(st, dict):
            return True
        # Explicit NO_HAT from daemon means blind (no trigger possible).
        if str(st.get("state") or "").upper() == "NO_HAT":
            return True
        try:
            ts = float(st.get("ts"))
        except Exception:
            return True
        return (time.time() - ts) > _UPS_STALE_S


class AIVisionPage(Page):
    name = "AI VISION"

    # Preview bezel (screen px, pre-supersample). The published frame is
    # 800x450; we paste it at (PREV_X, PREV_Y) scaled by SS with NEAREST
    # (the canvas is 2x; a clean integer scale keeps boxes/labels crisp).
    PREV_W, PREV_H = 800, 450
    PREV_X, PREV_Y = 40, 120
    BEZEL_PAD = 8                    # bezel rect inset around the preview

    # Right info column (chips + detection list) beside the preview.
    SIDE_X0 = 880
    SIDE_X1 = 1248

    # On-page power button (top-right): toggles the rubyvision service on/off.
    PWR_X0, PWR_Y0, PWR_X1, PWR_Y1 = 1058, 70, 1248, 110

    # Bottom chip strip (model / source / fps / temp / state).
    CHIP_Y = 590
    CHIP_X = 40
    # Right edge of the rendered chip strip (screen px), updated each frame by
    # _chip_strip; the cycle_model tap zone is bounded to this so it matches the
    # visible chips rather than spanning empty space out to SIDE_X1. Seeded to a
    # sane extent for taps that arrive before the first render.
    _chip_x_end = 880

    # ---- static chrome ---------------------------------------------------
    def render_static(self, draw, img):
        # Title, baseline-aligned with the other pages' header band.
        gauges.tracked_text(draw, 52 * SS, 84 * SS, "AI VISION",
                            font(28 * SS, "bold"), TEXT, tracking=4 * SS)

        # Preview bezel: a recessed dark plate behind the frame so an OFFLINE
        # / letterboxed preview reads as a deliberate viewport, not a gap.
        bx0 = (self.PREV_X - self.BEZEL_PAD) * SS
        by0 = (self.PREV_Y - self.BEZEL_PAD) * SS
        bx1 = (self.PREV_X + self.PREV_W + self.BEZEL_PAD) * SS
        by1 = (self.PREV_Y + self.PREV_H + self.BEZEL_PAD) * SS
        draw.rounded_rectangle([bx0, by0, bx1, by1], radius=12 * SS,
                               fill=mix(BG, PANEL, 0.5),
                               outline=CARD_BORDER, width=SS)
        draw.line([(bx0 + 12 * SS, by0 + SS), (bx1 - 12 * SS, by0 + SS)],
                  fill=CARD_EDGE, width=SS)

        # Right info card (detection list lives here per frame).
        gauges.card(draw, self.SIDE_X0 * SS, self.PREV_Y * SS,
                    self.SIDE_X1 * SS, (self.PREV_Y + self.PREV_H) * SS,
                    radius=14 * SS, scale=SS)
        gauges.tracked_text(draw, (self.SIDE_X0 + 20) * SS,
                            (self.PREV_Y + 14) * SS, "DETECTIONS",
                            font(17 * SS, "bold"), TEXT_DIM, tracking=3 * SS)
        draw.line([((self.SIDE_X0 + 12) * SS, (self.PREV_Y + 46) * SS),
                   ((self.SIDE_X1 - 12) * SS, (self.PREV_Y + 46) * SS)],
                  fill=CARD_BORDER, width=SS)

        # Chip-strip separator above the bottom info chips.
        draw.line([(self.CHIP_X * SS, (self.CHIP_Y - 14) * SS),
                   (self.SIDE_X1 * SS, (self.CHIP_Y - 14) * SS)],
                  fill=CARD_BORDER, width=SS)

    # ---- dynamic ---------------------------------------------------------
    def render(self, draw, img, snap, ctx):
        # On-page power toggle, drawn first so it shows in both live + offline
        # states (the offline card never reaches the top-right header).
        self._draw_power(draw)
        vc = ctx.get("vision")
        if vc is None:
            vc = VisionClient()
            ctx["vision"] = vc
        st = vc.status()
        offline = vc.offline(st)

        if offline:
            self._render_offline(draw, img, st)
            return

        state = str(st.get("state") or "")
        mode = str(st.get("mode") or "")
        source = str(st.get("source") or "")
        dets = st.get("detections") or []

        # Paste the latest annotated frame into the bezel (NEAREST keeps the
        # service-drawn boxes/labels crisp at the 2x canvas scale).
        self._paste_preview(img, vc, st)

        # Degraded-mode badge inside the preview (top-left). The service also
        # burns a DEMO badge into the JPEG; this is the HUD-side, palette-
        # correct echo so the mode is unmistakable even if the frame stalls.
        badge = self._badge_for(state, mode, source)
        if badge is not None:
            self._draw_badge(draw, badge[0], badge[1])

        self._chip_strip(draw, st, state, mode, source)
        self._detection_list(draw, dets)

    def _draw_power(self, draw):
        """Top-right on/off button for the rubyvision service (live + offline)."""
        from . import visionctl
        on = visionctl.is_active()
        col = OK if on else DANGER
        x0, y0 = self.PWR_X0 * SS, self.PWR_Y0 * SS
        x1, y1 = self.PWR_X1 * SS, self.PWR_Y1 * SS
        draw.rounded_rectangle([x0, y0, x1, y1], radius=10 * SS,
                               fill=mix(BG, col, 0.18), outline=col, width=2 * SS)
        gauges._centered_text(draw, (x0 + x1) // 2, (y0 + y1) // 2,
                              "VISION  ON" if on else "VISION  OFF",
                              font(20 * SS, "bold"), col)

    # -- preview paste -----------------------------------------------------
    def _paste_preview(self, img, vc, st):
        frame = vc.frame(st)
        x = self.PREV_X * SS
        y = self.PREV_Y * SS
        w = self.PREV_W * SS
        h = self.PREV_H * SS
        if frame is None:
            # Status is live but no decoded frame yet: dark viewport + hint.
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            d.rectangle([x, y, x + w, y + h], fill=mix(BG, PANEL, 0.3))
            gauges._centered_text(d, x + w // 2, y + h // 2, "WAITING FOR FRAME",
                                  font(26 * SS, "bold"), TEXT_DIM)
            return
        try:
            from PIL import Image
            scaled = frame.resize((w, h), Image.NEAREST)
            img.paste(scaled, (x, y))
        except Exception:
            pass

    # -- badge -------------------------------------------------------------
    @staticmethod
    def _badge_for(state, mode, source):
        """Return (text, color) for the in-preview mode badge, or None.

        Four visually-distinct degraded states (OFFLINE handled separately):
          no_camera          -> amber "DEMO - NO CAMERA"
          stub mode          -> amber "DEMO - CPU STUB"
          pattern/video src  -> amber "DEMO" (synthetic input)
          ok + hailo + camera -> no badge (live)."""
        if state == "no_camera":
            return ("DEMO - NO CAMERA", WARN)
        if mode and mode != "hailo":
            return ("DEMO - CPU STUB", WARN)
        if source in ("pattern", "video"):
            return ("DEMO", WARN)
        return None

    def _draw_badge(self, draw, text, color):
        x = (self.PREV_X + 10) * SS
        y = (self.PREV_Y + 10) * SS
        gauges.status_chip(draw, x, y, text, color, filled=True, scale=SS)

    # -- bottom chip strip -------------------------------------------------
    def _chip_strip(self, draw, st, state, mode, source):
        live = (state == "ok" and mode == "hailo"
                and source not in ("pattern", "video"))
        ok_col = OK if live else WARN

        model = str(st.get("model") or "?")
        inf = _num(st.get("inference_fps"))
        htemp = _num(st.get("hailo_temp_c"))

        chips = []
        chips.append(("MODEL " + model[:18], ok_col))
        chips.append(("SRC " + (source or "?").upper(), ok_col))
        chips.append(("INF %d fps" % int(round(inf)) if inf is not None
                      else "INF -- fps", TEXT_DIM))
        chips.append(("HAILO %d %s" % (int(round(_disp_temp(htemp))),
                                       config.temp_label())
                      if htemp is not None else "HAILO --", TEXT_DIM))
        chips.append((("LIVE" if live else (state or "?").upper()),
                      OK if live else WARN))

        x = self.CHIP_X * SS
        y = self.CHIP_Y * SS
        for txt, col in chips:
            w, _ = gauges.status_chip(draw, x, y, txt, col,
                                      filled=(txt.startswith("MODEL")
                                              or txt in ("LIVE",)),
                                      scale=SS)
            x += w + 12 * SS
        # Record the visible right edge (back in screen px) for tap bounding.
        self._chip_x_end = int(x / SS)

    # -- detection list ----------------------------------------------------
    def _detection_list(self, draw, dets):
        x0 = (self.SIDE_X0 + 20) * SS
        x1 = (self.SIDE_X1 - 20) * SS
        y = (self.PREV_Y + 60) * SS
        rfont = font(20 * SS, "mono")

        try:
            top = sorted(dets, key=lambda d: float(d.get("conf", 0.0)),
                         reverse=True)
        except Exception:
            top = list(dets)

        # Count chip at the bottom of the card.
        cy = (self.PREV_Y + self.PREV_H - 40) * SS
        gauges.status_chip(draw, x0, cy, "%d OBJECTS" % len(top),
                           ACCENT if top else TEXT_DIM,
                           filled=bool(top), scale=SS)

        if not top:
            gauges._centered_text(draw, (x0 + x1) // 2,
                                  (self.PREV_Y + 240) * SS, "NONE",
                                  font(26 * SS, "bold"), TEXT_DIM)
            return

        row_h = 40 * SS
        max_rows = 8
        for d in top[:max_rows]:
            try:
                cls = str(d.get("cls") or "?")[:14]
                conf = float(d.get("conf", 0.0))
            except Exception:
                continue
            draw.text((x0, y), cls, font=rfont, fill=TEXT)
            pct = "%d%%" % int(round(max(0.0, min(1.0, conf)) * 100))
            try:
                pw = draw.textlength(pct, font=rfont)
            except Exception:
                pw = gauges._text_size(draw, pct, rfont)[0]
            draw.text((x1 - pw, y), pct, font=rfont, fill=ACCENT_GLOW)
            y += row_h
        extra = len(top) - max_rows
        if extra > 0:
            draw.text((x0, y), "+%d more" % extra, font=font(18 * SS, "bold"),
                      fill=TEXT_DIM)

    # -- offline card ------------------------------------------------------
    def _render_offline(self, draw, img, st):
        """Full-preview OFFLINE card: dim plate + DANGER title + hint. Used
        when the service is down or its status is stale (> 2s)."""
        x = self.PREV_X * SS
        y = self.PREV_Y * SS
        w = self.PREV_W * SS
        h = self.PREV_H * SS
        draw.rectangle([x, y, x + w, y + h], fill=mix(BG, PANEL, 0.3))

        cx = x + w // 2
        cy = y + h // 2
        # Soft DANGER glow behind the title.
        g = gauges.glow_dot(60 * SS, DANGER, strength=0.5)
        img.paste(g, (cx - g.width // 2, cy - 40 * SS - g.height // 2), g)
        gauges._centered_text(draw, cx, cy - 30 * SS,
                              "VISION SERVICE OFFLINE",
                              font(38 * SS, "bold"), DANGER)
        hint = "rubyvision not running or stale"
        if isinstance(st, dict) and st.get("error"):
            hint = str(st.get("error"))[:48]
        gauges._centered_text(draw, cx, cy + 24 * SS, hint,
                              font(22 * SS, "regular"), TEXT_DIM)
        gauges._centered_text(draw, cx, cy + 70 * SS,
                              "systemctl status rubyvision",
                              font(20 * SS, "mono"), mix(BG, TEXT_DIM, 0.75))

        # Dim the side card contents to match (no live detections).
        gauges.status_chip(draw, (self.SIDE_X0 + 20) * SS,
                           (self.PREV_Y + 60) * SS, "NO DATA", TEXT_DIM,
                           scale=SS)
        # Offline chip strip.
        gauges.status_chip(draw, self.CHIP_X * SS, self.CHIP_Y * SS,
                           "OFFLINE", DANGER, filled=True, scale=SS)

    # ---- input -----------------------------------------------------------
    def handle_tap(self, x, y, ctx):
        # Power button (top-right): toggle the rubyvision service on/off.
        if (self.PWR_X0 <= x <= self.PWR_X1
                and self.PWR_Y0 <= y <= self.PWR_Y1):
            from . import visionctl
            visionctl.toggle()
            return True
        vc = ctx.get("vision")
        if vc is None:
            vc = VisionClient()
            ctx["vision"] = vc
        # Tap inside the preview -> cycle the capture source.
        if (self.PREV_X <= x <= self.PREV_X + self.PREV_W
                and self.PREV_Y <= y <= self.PREV_Y + self.PREV_H):
            vc.write_cmd("cycle_source")
            return True
        # Tap on the bottom chip strip -> cycle the model. Bound to the actual
        # rendered chip extent (not SIDE_X1) so empty space to the right of the
        # last chip / below the DETECTIONS card is not a hidden tap target.
        if (self.CHIP_X <= x <= self._chip_x_end
                and self.CHIP_Y - 14 <= y <= self.CHIP_Y + 44):
            vc.write_cmd("cycle_model")
            return True
        return False


# --------------------------------------------------------------------------- #
# Page: BODY — top-down line-art MX-5 with door / trunk / blind-spot status
# --------------------------------------------------------------------------- #
class BodyView(Page):
    """Calm top-down body view in the Tesla language: a hairline MX-5 with open
    panels (driver/passenger door, trunk) lit in red and a blind-spot arc on
    any active side. Roof state shown as a small badge. No takeover, no spin."""

    name = "BODY"

    def render_static(self, draw, img):
        gauges.tracked_text(draw, 40 * SS, 84 * SS, "BODY",
                            font(26 * SS, "bold"), TEXT, tracking=8 * SS)
        gauges.tracked_text(draw, 40 * SS, 116 * SS, "ND1 MX-5 RF",
                            font(14 * SS, "regular"), TEXT_DIM, tracking=4 * SS)

    def render(self, draw, img, snap, ctx):
        from . import bodycar
        bodycar.draw_car(img, draw, snap, SS, cx=img.width // 2,
                         cy=int(402 * SS), car_len=536)
        self._roof_badge(draw, snap)
        self._caption(draw, snap)

    @staticmethod
    def _roof_badge(draw, snap):
        roof = snap.roof or "-"
        col = OK if roof == "CLOSED" else (TEXT_DIM if roof == "-" else WARN)
        gauges.tracked_text(draw, 1240 * SS, 90 * SS, "ROOF",
                            font(13 * SS, "regular"), TEXT_DIM, tracking=4 * SS,
                            anchor="ra")
        gauges.tracked_text(draw, 1240 * SS, 116 * SS,
                            ("--" if roof == "-" else roof),
                            font(20 * SS, "regular"), col, tracking=2 * SS,
                            anchor="ra")

    @staticmethod
    def _caption(draw, snap):
        opens = []
        if snap.door_left:
            opens.append("Driver door")
        if snap.door_right:
            opens.append("Passenger door")
        if snap.trunk:
            opens.append("Trunk")
        if opens:
            msg, col = "  ·  ".join(opens) + " open", DANGER
        else:
            msg, col = "ALL CLOSED", mix(theme.BG, TEXT_DIM, 0.3)
        gauges.tracked_text_center(draw, SW // 2, 690 * SS, msg,
                                   font(23 * SS, "regular"), col, tracking=3 * SS)
        if snap.bsm_left or snap.bsm_right:
            side = "Both sides" if (snap.bsm_left and snap.bsm_right) else (
                "Left" if snap.bsm_left else "Right")
            gauges.tracked_text_center(draw, SW // 2, 718 * SS,
                                       "VEHICLE IN BLIND SPOT · " + side.upper(),
                                       font(14 * SS, "bold"), DANGER,
                                       tracking=2 * SS)


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #
def make_pages() -> list:
    from .settings import SettingsPage  # function-level: no import cycle
    # Visible swipe rotation: GAUGES, VEHICLE, BODY, SYSTEM, CONFIGURE[, AI].
    pages = [GaugesPage(), VehiclePage(), BodyView(), SystemPage(),
             SettingsPage()]
    # Vision page appended to the visible rotation if it constructs.
    try:
        vision = AIVisionPage()
    except Exception:
        vision = None
    if vision is not None:
        pages.append(vision)
    # Hidden pages: not in the swipe rotation, reached via CONFIGURE deep-links
    # (ctx['nav_request']). CanPage + PlaybackPage are diagnostic / on-demand.
    pages.append(CanPage())
    try:
        from .playback import PlaybackPage
        pages.append(PlaybackPage())
    except Exception:
        pass
    try:
        from .wifipage import WiFiPage
        pages.append(WiFiPage())
    except Exception:
        pass
    try:
        from .bluetoothpage import BluetoothPage
        pages.append(BluetoothPage())
    except Exception:
        pass
    return pages


def make_ctx(channel: str) -> dict:
    return {"theme": theme, "gauges": gauges, "channel": channel,
            "frozen": False, "cpu_hist": deque(maxlen=60), "vision": None,
            # deep-link request: a menu action sets this to a page name; the
            # main loop consumes it to switch to that (often hidden) page.
            "nav_request": None}
