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

import os
import shutil
import time
from collections import deque

from . import gauges, theme
from .render import (COOLANT_HI, COOLANT_LO, RPM_MAX, RPM_REDLINE, SS, SW,
                     VOLTS_HI, VOLTS_LO, _frac, _num)
from .signals import _run
from .theme import (ACCENT, ACCENT_GLOW, CARD_BORDER, DANGER, OK, PANEL,
                    ROW_A, ROW_B, TEXT, TEXT_DIM, TICK, WARN, font, mix)

BG = theme.BG


class Page:
    """Base page: render the body; handle_tap returns True when consumed."""

    name = "PAGE"

    def render_static(self, draw, img):
        """Draw never-changing page chrome into the static layer (once)."""
        pass

    def render(self, draw, img, snap, ctx):
        pass

    def handle_tap(self, x, y, ctx):
        return False


# --------------------------------------------------------------------------- #
# Page 1: gauges (the hero screen)
# --------------------------------------------------------------------------- #
class GaugesPage(Page):
    name = "GAUGES"

    # Dial geometry (screen px, pre-supersample).
    DIAL_CX, DIAL_CY, DIAL_R = 380, 430, 260
    REDLINE_FROM = 6500.0  # redline sector start (design spec)

    # Speed hero block.
    SPD_RIGHT, SPD_BASE = 950, 392
    GEAR_CX, GEAR_CY, GEAR_SIZE = 870, 540, 120

    # Right pill meters (clear of the MPH unit and the right chevron).
    PILL_Y, PILL_H, PILL_W = 120, 400, 36
    PILL_XS = (1088, 1148, 1208)

    def render_static(self, draw, img):
        cx, cy, r = self.DIAL_CX * SS, self.DIAL_CY * SS, self.DIAL_R * SS
        gauges.dial_static(img, draw, cx, cy, r,
                           self.REDLINE_FROM / RPM_MAX, rpm_max=RPM_MAX,
                           scale=SS)

        # Speed unit, baseline-aligned with the (dynamic) numerals.
        try:
            draw.text(((self.SPD_RIGHT + 12) * SS, self.SPD_BASE * SS),
                      "MPH", font=font(40 * SS, "bold"), fill=TEXT_DIM,
                      anchor="ls")
        except Exception:
            draw.text(((self.SPD_RIGHT + 12) * SS,
                       (self.SPD_BASE - 34) * SS), "MPH",
                      font=font(40 * SS, "bold"), fill=TEXT_DIM)

        # Gear plate (letter is dynamic).
        gauges.gear_plate(draw, self.GEAR_CX * SS, self.GEAR_CY * SS,
                          self.GEAR_SIZE * SS)

        # Pill meter chrome.
        y, h, w = self.PILL_Y * SS, self.PILL_H * SS, self.PILL_W * SS
        specs = (
            ("COOL", "C", self._coolant_markers(False)),
            ("VOLT", "V", self._volt_markers(False)),
            ("THR", "%", None),
        )
        for x, (label, unit, markers) in zip(self.PILL_XS, specs):
            gauges.pill_static(img, draw, x * SS, y, w, h, label, unit,
                               markers=markers, scale=SS)

    @staticmethod
    def _coolant_markers(value):
        hot = _num(value) is not None and float(value) > 100
        vhot = _num(value) is not None and float(value) > 110
        return [
            (_frac(100, COOLANT_LO, COOLANT_HI), WARN, hot and not vhot),
            (_frac(110, COOLANT_LO, COOLANT_HI), DANGER, vhot),
        ]

    @staticmethod
    def _volt_markers(value):
        low = _num(value) is not None and float(value) < 11.8
        vlow = _num(value) is not None and float(value) < 11.2
        return [
            (_frac(11.8, VOLTS_LO, VOLTS_HI), WARN, low and not vlow),
            (_frac(11.2, VOLTS_LO, VOLTS_HI), DANGER, vlow),
        ]

    def render(self, draw, img, snap, ctx):
        self._tach(draw, img, snap)
        self._speed_and_gear(draw, snap)
        self._right_meters(draw, img, snap)
        self._warnings(draw, img, snap)

    def _tach(self, draw, img, snap):
        cx, cy, r = self.DIAL_CX * SS, self.DIAL_CY * SS, self.DIAL_R * SS
        frac = _frac(snap.rpm, 0, RPM_MAX)
        gauges.dial_sweep(img, cx, cy, r, frac)
        gauges.dial_needle(img, draw, cx, cy, r, frac)
        rpm = _num(snap.rpm)
        vt = "--" if rpm is None else "%d" % int(round(rpm))
        in_red = rpm is not None and float(rpm) >= self.REDLINE_FROM
        gauges._centered_text(draw, cx, cy - int(r * 0.10), vt,
                              font(int(r * 0.30), "bold"),
                              DANGER if in_red else TEXT)

    def _speed_and_gear(self, draw, snap):
        speed = _num(snap.speed_mph)
        spd = "--" if speed is None else "%d" % int(round(speed))
        gauges.kerned_right(draw, self.SPD_RIGHT * SS, self.SPD_BASE * SS,
                            spd, font(170 * SS, "bold"), TEXT,
                            tracking=-8 * SS)
        gauges.gear_value(draw, self.GEAR_CX * SS, self.GEAR_CY * SS,
                          snap.gear, self.GEAR_SIZE * SS)

    def _right_meters(self, draw, img, snap):
        y, h, w = self.PILL_Y * SS, self.PILL_H * SS, self.PILL_W * SS

        coolant = _num(snap.coolant_c)
        cval = None if coolant is None else "%d" % int(round(coolant))
        gauges.pill_fill(img, draw, self.PILL_XS[0] * SS, y, w, h,
                         _frac(snap.coolant_c, COOLANT_LO, COOLANT_HI),
                         cval, markers=self._coolant_markers(coolant),
                         scale=SS)

        volts = _num(snap.volts)
        vval = None if volts is None else "%.1f" % float(volts)
        gauges.pill_fill(img, draw, self.PILL_XS[1] * SS, y, w, h,
                         _frac(snap.volts, VOLTS_LO, VOLTS_HI),
                         vval, markers=self._volt_markers(volts), scale=SS)

        throttle = _num(snap.throttle_pct)
        tval = None if throttle is None else "%d" % int(round(throttle))
        gauges.pill_fill(img, draw, self.PILL_XS[2] * SS, y, w, h,
                         _frac(snap.throttle_pct, 0, 100), tval, scale=SS)

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
# Page 2: raw CAN traffic
# --------------------------------------------------------------------------- #
class CanPage(Page):
    name = "CAN BUS"

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

    # Tile grid (screen px, pre-supersample): 2 cols x 3 rows in the body.
    TILE_W, TILE_H = 596, 196
    GRID_X, GRID_Y = 32, 100
    GAP_X, GAP_Y = 24, 12

    # CPU sparkline area within tile 0 (offsets in screen px).
    SPARK_X0, SPARK_X1 = 320, 572
    SPARK_Y0, SPARK_Y1 = 64, 124

    _GLYPHS = (_glyph_cpu, _glyph_temp, _glyph_mem, _glyph_disk,
               _glyph_wifi, _glyph_bolt)
    _TITLES = ("CPU", "TEMP", "MEM", "DISK /", "NETWORK", "POWER EXT5V")

    def _cells(self):
        return [(self.GRID_X + c * (self.TILE_W + self.GAP_X),
                 self.GRID_Y + r * (self.TILE_H + self.GAP_Y))
                for r in range(3) for c in range(2)]

    def render_static(self, draw, img):
        cells = self._cells()
        for i, cell in enumerate(cells):
            x, y = cell[0] * SS, cell[1] * SS
            w, h = self.TILE_W * SS, self.TILE_H * SS
            gauges.card(draw, x, y, x + w, y + h, radius=14 * SS, scale=SS)
            gauges.tracked_text(draw, x + 24 * SS, y + 16 * SS,
                                self._TITLES[i], font(19 * SS, "bold"),
                                TEXT_DIM, tracking=2 * SS)
            self._GLYPHS[i](draw, x + w - 52 * SS, y + 44 * SS, 40 * SS)
        # Sparkline baseline (CPU tile).
        x, y = cells[0][0] * SS, cells[0][1] * SS
        draw.line([(x + self.SPARK_X0 * SS, y + self.SPARK_Y1 * SS),
                   (x + self.SPARK_X1 * SS, y + self.SPARK_Y1 * SS)],
                  fill=TICK, width=SS)

    def render(self, draw, img, snap, ctx):
        cells = self._cells()

        cpu_pct, load = get_cpu()
        hist = ctx.get("cpu_hist")
        if hist is None:
            hist = deque(maxlen=60)
            ctx["cpu_hist"] = hist
        if cpu_pct is not None:
            hist.append(float(cpu_pct))
        cpu_v = None if cpu_pct is None else "%d%%" % int(round(cpu_pct))
        self._tile(draw, cells[0], cpu_v, TEXT,
                   None if load is None else "load " + load)
        self._sparkline(draw, cells[0], hist)

        temp = get_temp_c()
        tv, tcol = None, TEXT
        if temp is not None:
            tv = "%d C" % int(round(temp))
            tcol = DANGER if temp > 80 else (WARN if temp > 70 else OK)
        self._tile(draw, cells[1], tv, tcol, None)

        mem = get_mem()
        mv = msub = None
        if mem is not None:
            mv = "%.1f GB" % mem[0]
            msub = "of %.1f GB used" % mem[1]
        self._tile(draw, cells[2], mv, TEXT, msub)

        disk = get_disk()
        dv = dsub = None
        if disk is not None:
            dv = "%d%%" % int(round(disk[2]))
            dsub = "%.0f / %.0f GB used" % (disk[0], disk[1])
        self._tile(draw, cells[3], dv, TEXT, dsub)

        ssid, ips = get_net()
        nv = (ssid or "--")[:14]
        nsub = "TS %s   %s" % (snap.tailscale or "?", ips or "--")
        self._tile(draw, cells[4], nv, TEXT, nsub)

        volts = get_ext5v()
        pv, pcol = None, TEXT
        if volts is not None:
            pv = "%.2f V" % volts
            pcol = DANGER if volts < 4.8 else (WARN if volts < 4.95 else OK)
        self._tile(draw, cells[5], pv, pcol, None,
                   badges=self._power_badges())

    def _sparkline(self, draw, cell, hist):
        """Last 60 CPU samples as a cheap accent polyline."""
        if not hist or len(hist) < 2:
            return
        x = (cell[0] + self.SPARK_X0) * SS
        y0 = (cell[1] + self.SPARK_Y0) * SS
        y1 = (cell[1] + self.SPARK_Y1) * SS
        w = (self.SPARK_X1 - self.SPARK_X0) * SS
        n = hist.maxlen or 60
        step = w / float(max(1, n - 1))
        pts = []
        for i, v in enumerate(hist):
            v = max(0.0, min(100.0, float(v)))
            pts.append((x + i * step, y1 - (y1 - y0) * v / 100.0))
        draw.line(pts, fill=ACCENT, width=2 * SS)
        # Brighter dot on the newest sample.
        px, py = pts[-1]
        r = 4 * SS
        draw.ellipse([px - r, py - r, px + r, py + r], fill=ACCENT_GLOW)

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

    def _tile(self, draw, cell, value, vcol, sub, badges=None):
        """Dynamic tile content (card chrome + title live in the static
        layer; see render_static)."""
        x, y = cell[0] * SS, cell[1] * SS
        vt = "--" if value is None else str(value)
        draw.text((x + 24 * SS, y + 52 * SS), vt,
                  font=font(46 * SS, "mono"), fill=vcol or TEXT)
        if sub:
            draw.text((x + 24 * SS, y + 138 * SS), str(sub),
                      font=font(19 * SS, "regular"), fill=TEXT_DIM)
        bx = x + 24 * SS
        for txt, col in (badges or []):
            bw, _ = gauges.status_chip(draw, bx, y + 128 * SS, txt, col,
                                       scale=SS)
            bx += bw + 12 * SS


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #
def make_pages() -> list:
    return [GaugesPage(), CanPage(), SystemPage()]


def make_ctx(channel: str) -> dict:
    return {"theme": theme, "gauges": gauges, "channel": channel,
            "frozen": False, "cpu_hist": deque(maxlen=60)}
