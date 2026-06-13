"""Page 4: SETTINGS (self-update controls on top of TouchMenu).

Every side effect goes through updates.request() (JSON request files
consumed by the root ruby-updated handler); the page itself never touches
git or systemd. While an apply/rollback is in flight the row list is
replaced by a progress panel (big phase name + spinner + the last
status.jsonl lines) and a terminal phase shows a result banner for 5
minutes. __init__ re-enters progress/result from status.json, so the page
survives the HUD restart that happens in the middle of every update.

Mode detection: apply/rollback statuses carry "target"; check statuses do
not, so a plain version check never hijacks the screen.
"""

from __future__ import annotations

import os
import time

from . import config, gauges, recorder, updates
from .menu import MenuItem, TouchMenu
from .render import SS
from .theme import (ACCENT_GLOW, BG, DANGER, OK, TEXT, TEXT_DIM, WARN, font,
                    mix)

_TERMINAL = ("done", "error", "rolled-back", "busy")
_RESULT_S = 300.0   # how long a terminal result banner sticks around
_WAIT_S = 120.0     # show QUEUED this long while the daemon hasn't written
_STALE_S = 1800.0   # max age of a non-terminal status before it's "wedged"
                    # (matches the daemon's 30-min health-pending window and
                    # sits above the longest silent phase: deps 300s + watch 90s)
_REQ_ERR_S = 4.0    # how long the "request not sent" banner shows


def _phase(st) -> str:
    return str((st or {}).get("phase") or "")


def _terminal(phase: str) -> bool:
    # startswith covers "rolled-back (auto)".
    return any(phase == p or phase.startswith(p) for p in _TERMINAL)


def _release_value():
    lr = updates.last_result() or {}
    ref = lr.get("ref")
    if not ref:
        return "--"
    sha = str(lr.get("sha") or "")[:9]
    return "%s @ %s" % (ref, sha) if sha else str(ref)


def _applied_value():
    lr = updates.last_result() or {}
    try:
        ts = float(lr.get("ts"))
    except Exception:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _uptime_value():
    try:
        with open("/proc/uptime") as fh:
            s = float(fh.read().split()[0])
    except Exception:
        return "--"
    d, rem = divmod(int(s), 86400)
    h, rem = divmod(rem, 3600)
    if d:
        return "%dd %02d:%02d" % (d, h, rem // 60)
    return "%02d:%02d" % (h, rem // 60)


class SettingsPage(TouchMenu):
    name = "CONFIGURE"

    def __init__(self):
        super().__init__("CONFIGURE", self._root_items())
        self._check_wall = None   # wall time of our last check request
        self._flow_wall = None    # wall time of our apply/rollback request
        self._result_seen = None  # status ts of a dismissed result banner
        self._req_err = None      # monotime a request() returned False (banner)
        # Re-enter progress/result across a mid-update HUD restart, but only
        # for a *recent* status: a stale non-terminal status.json (e.g. an
        # updater killed mid-apply, leaving "deps" in /run until reboot) must
        # not lock the page into the progress panel forever.
        st = updates.status()
        if st is not None and st.get("target"):
            try:
                ts = float(st.get("ts") or 0)
            except Exception:
                ts = 0.0
            ph = _phase(st)
            now = time.time()
            if not _terminal(ph):
                if ts and now - ts < _STALE_S:
                    self._flow_wall = ts
                # else: wedged/old in-flight status -> stay in menu mode
            elif ts and now - ts < _RESULT_S:
                self._flow_wall = ts

    # -- items ---------------------------------------------------------------- #
    def _root_items(self) -> list:
        # Update controls stay at the TOP (used every release); the new config
        # items follow; about/satellite/service at the end. CAN BUS / PLAYBACK
        # are hidden pages reached via deep-link.
        return [
            MenuItem("CHECK FOR UPDATES", value_fn=self._check_value,
                     on_tap=self._do_check),
            MenuItem("UPDATE NOW", value_fn=self._avail_value,
                     enabled_fn=self._update_ready,
                     confirm=self._confirm_apply, on_tap=self._do_apply),
            MenuItem("ROLLBACK", value_fn=self._prev_value,
                     enabled_fn=lambda: updates.previous_version() is not None,
                     confirm=self._confirm_rollback, danger=True,
                     on_tap=self._do_rollback),
            MenuItem("TEMPERATURE", value_fn=config.temp_label,
                     on_tap=lambda ctx: config.toggle_temp_unit()),
            MenuItem("SPEED UNITS", value_fn=config.speed_label,
                     on_tap=lambda ctx: config.toggle_speed_unit()),
            MenuItem("CUSTOMIZE SCREENS", submenu=self._satellite_items),
            MenuItem("WI-FI", submenu=self._wifi_items),
            MenuItem("RECORDING", value_fn=self._recording_value,
                     submenu=self._recording_items),
            MenuItem("CAN BUS",
                     on_tap=lambda ctx: ctx.__setitem__("nav_request", "CAN BUS")),
            # Subsystems with hardware/feasibility prerequisites: surfaced as
            # honest 'planned' entries (dimmed) until each is built out.
            MenuItem("PHONE CONNECTION", value_fn=lambda: "planned",
                     enabled_fn=lambda: False),
            MenuItem("BLUETOOTH", value_fn=lambda: "planned",
                     enabled_fn=lambda: False),
            MenuItem("CARPLAY", value_fn=lambda: "planned",
                     enabled_fn=lambda: False),
            MenuItem("NAVIGATION", value_fn=lambda: "planned",
                     enabled_fn=lambda: False),
            MenuItem("VERSION / ABOUT", submenu=self._about_items),
            MenuItem("SATELLITE", submenu=self._satellite_items),
            MenuItem("SERVICE", submenu=self._service_items),
        ]

    @staticmethod
    def _recording_value():
        return "REC" if recorder.any_active() else "off"

    def _recording_items(self) -> list:
        return [
            MenuItem("RECORD SCREEN", value_fn=recorder.screen_status,
                     on_tap=lambda ctx: recorder.toggle_screen()),
            MenuItem("RECORD CAMERA", value_fn=recorder.camera_status,
                     on_tap=lambda ctx: recorder.toggle_camera()),
            MenuItem("PLAYBACK", value_fn=recorder.recording_count,
                     on_tap=lambda ctx: ctx.__setitem__("nav_request",
                                                        "PLAYBACK")),
            MenuItem("LAST FILE", value_fn=recorder.last_file_name),
            MenuItem("SAVED TO", value_fn=lambda: "~/recordings"),
        ]

    @staticmethod
    def _wifi_items() -> list:
        # value_fn runs every frame on the render thread, so the (expensive)
        # iwgetid/hostname/nmcli calls are TTL-cached -- they re-run at most once
        # every few seconds, never per frame. Fresh cache per submenu open.
        from .signals import _run

        cache: dict = {}

        def cached(key, ttl, fn):
            now = time.monotonic()
            ent = cache.get(key)
            if ent is not None and now - ent[1] < ttl:
                return ent[0]
            try:
                v = fn()
            except Exception:
                v = "--"
            cache[key] = (v, now)
            return v

        def ssid():
            return cached("ssid", 4.0, lambda: (
                (_run(["iwgetid", "-r"], timeout=2.0) or "").strip() or "--"))

        def ip():
            def _q():
                parts = (_run(["hostname", "-I"], timeout=2.0) or "").split()
                return parts[0] if parts else "--"
            return cached("ip", 4.0, _q)

        def sig():
            def _q():
                out = _run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "dev", "wifi"],
                           timeout=2.0)
                for line in (out or "").splitlines():
                    if line.startswith("yes:"):
                        return line.split(":", 1)[1].strip() + "%"
                return "--"
            return cached("sig", 6.0, _q)

        return [
            MenuItem("SSID", value_fn=ssid),
            MenuItem("IP", value_fn=ip),
            MenuItem("SIGNAL", value_fn=sig),
        ]

    # -- satellite (4" dash HUD) control ------------------------------------- #
    _SAT_CTL = "/dev/shm/rubysat-ctl.json"
    _sat_seq = [0]

    def _sat_send(self, cmd: str):
        """Atomic write rubysat picks up and rides to the Qualia. Never raises."""
        try:
            import json as _json
            import tempfile as _tf
            self._sat_seq[0] += 1
            payload = _json.dumps({"seq": self._sat_seq[0], "cmd": cmd,
                                   "ts": round(time.time(), 3)})
            d = os.path.dirname(self._SAT_CTL)
            fd, tmp = _tf.mkstemp(prefix=".sct-", dir=d)
            try:
                os.write(fd, payload.encode("ascii"))
            finally:
                os.close(fd)
            os.replace(tmp, self._SAT_CTL)
        except Exception:
            pass

    def _satellite_items(self) -> list:
        return [
            MenuItem("HUD MIRROR",
                     on_tap=lambda ctx: self._sat_send("mirror_toggle")),
            MenuItem("SHOW GAUGES",
                     on_tap=lambda ctx: self._sat_send("sat_page0")),
            MenuItem("SHOW STATUS",
                     on_tap=lambda ctx: self._sat_send("sat_page1")),
            MenuItem("SHOW MENU",
                     on_tap=lambda ctx: self._sat_send("sat_page2")),
            MenuItem("< PREV PAGE",
                     on_tap=lambda ctx: self._sat_send("sat_prev")),
            MenuItem("NEXT PAGE >",
                     on_tap=lambda ctx: self._sat_send("sat_next")),
            MenuItem("ROTATE 180",
                     on_tap=lambda ctx: self._sat_send("rot_toggle")),
            MenuItem("BACKLIGHT OFF", danger=True,
                     confirm="Turn the 4-inch backlight off?",
                     on_tap=lambda ctx: self._sat_send("backlight_off")),
            MenuItem("BACKLIGHT ON",
                     on_tap=lambda ctx: self._sat_send("backlight_on")),
        ]

    @staticmethod
    def _about_items() -> list:
        return [
            MenuItem("VERSION", value_fn=updates.current_version),
            MenuItem("RELEASE", value_fn=_release_value),
            MenuItem("APPLIED", value_fn=_applied_value),
            MenuItem("CHANNEL", value_fn=lambda: "latest tag"),
            MenuItem("UPTIME", value_fn=_uptime_value),
            MenuItem("UPDATER", value_fn=lambda: (
                "armed" if updates.queue_writable() else "offline")),
        ]

    def _service_items(self) -> list:
        return [
            MenuItem("RESTART HUD", confirm="Restart the HUD now?",
                     on_tap=lambda ctx: self._req("restart-hud")),
            MenuItem("CONSOLE DASH", danger=True,
                     confirm="Switch to the console dash? The HUD stops.",
                     on_tap=lambda ctx: self._req("switch-dash")),
        ]

    # -- update actions / values ----------------------------------------------- #
    def _req(self, cmd, ref=None) -> bool:
        """Queue a request; flag a short-lived error banner on failure."""
        ok = updates.request(cmd, ref)
        if not ok:
            self._req_err = time.monotonic()
        return ok

    def _do_check(self, ctx):
        # Floor the anchor: the daemon stamps ts with an integer `date +%s`,
        # so a status written in the same wall-clock second as this tap must
        # still count as fresh (ts >= floor(now)), not be hidden as stale.
        if self._req("check"):
            self._check_wall = float(int(time.time()))

    def _check_value(self):
        st = updates.status()
        try:
            ts = float((st or {}).get("ts") or 0)
        except Exception:
            ts = 0.0
        if (self._check_wall is not None
                and time.time() - self._check_wall < 30.0
                and ts < self._check_wall):
            return "checking..."
        if st is None:
            return "offline"
        ph = _phase(st)
        if not _terminal(ph) and not st.get("target"):
            return "checking..."
        if ph == "error" and not st.get("target"):
            return "offline"
        cur, avail = st.get("current"), st.get("available")
        if avail and cur and avail != cur:
            return "%s available" % avail
        if avail or cur:
            return "up to date"
        return "--"

    def _update_ready(self):
        st = updates.status() or {}
        avail, cur = st.get("available"), st.get("current")
        return bool(avail) and avail != cur

    def _avail_value(self):
        st = updates.status() or {}
        avail, cur = st.get("available"), st.get("current")
        return str(avail) if avail and avail != cur else None

    def _confirm_apply(self):
        st = updates.status() or {}
        return ("Install %s? HUD will restart."
                % (st.get("available") or "latest"))

    def _do_apply(self, ctx):
        st = updates.status() or {}
        # Floor the anchor so a same-second daemon write (integer ts) is fresh.
        if self._req("apply", st.get("available")):
            self._flow_wall = float(int(time.time()))

    def _prev_value(self):
        return updates.previous_version() or "--"

    def _confirm_rollback(self):
        return ("Roll back to %s? HUD will restart."
                % (updates.previous_version() or "previous"))

    def _do_rollback(self, ctx):
        # Floor the anchor so a same-second daemon write (integer ts) is fresh.
        if self._req("rollback"):
            self._flow_wall = float(int(time.time()))

    # -- progress / result mode -------------------------------------------------- #
    def _mode(self):
        """('menu' | 'progress' | 'result', status dict or None)."""
        st = updates.status()
        if self._flow_wall is None:
            return ("menu", st)
        now = time.time()
        try:
            ts = float((st or {}).get("ts") or 0)
        except Exception:
            ts = 0.0
        fresh = st is not None and ts >= self._flow_wall
        ph = _phase(st)
        if fresh and not _terminal(ph):
            if now - ts >= _STALE_S:
                # A non-terminal status that stopped advancing (wedged updater)
                # must not hold the page hostage; release back to the menu.
                self._flow_wall = None
                return ("menu", st)
            return ("progress", st)
        if fresh:  # terminal
            if self._result_seen != ts and now - ts < _RESULT_S:
                return ("result", st)
            self._flow_wall = None
            return ("menu", st)
        if now - self._flow_wall < _WAIT_S:
            return ("progress", None)  # queued; daemon hasn't written yet
        self._flow_wall = None
        return ("menu", st)

    def _dismiss_result(self, st):
        try:
            self._result_seen = float((st or {}).get("ts") or 0)
        except Exception:
            self._result_seen = 0.0
        self._flow_wall = None

    def render(self, draw, img, snap, ctx):
        mode, st = self._mode()
        if mode == "progress":
            self._draw_progress(draw, st)
        elif mode == "result":
            self._draw_result(draw, st)
        else:
            super().render(draw, img, snap, ctx)
            if (self._req_err is not None
                    and time.monotonic() - self._req_err < _REQ_ERR_S):
                self._draw_req_err(draw)

    def _draw_req_err(self, draw):
        cx = ((self.MENU_X0 + self.MENU_X1) // 2) * SS
        gauges.tracked_text_center(
            draw, cx, 672 * SS, "UPDATER OFFLINE - REQUEST NOT SENT",
            font(15 * SS, "bold"), DANGER, tracking=2 * SS)

    def _draw_header(self, draw, tail):
        gauges.tracked_text(draw, (self.MENU_X0 + 28) * SS,
                            (self.CARD_Y0 + 13) * SS, "CONFIGURE > " + tail,
                            font(20 * SS, "bold"), TEXT_DIM, tracking=2 * SS)

    @staticmethod
    def _fmt_line(d) -> str:
        try:
            tstr = time.strftime("%H:%M:%S", time.localtime(float(d["ts"])))
        except Exception:
            tstr = "--:--:--"
        line = "%s %-11s %s" % (tstr, str(d.get("phase") or ""),
                                str(d.get("msg") or ""))
        return line[:70]

    def _draw_progress(self, draw, st):
        self._draw_header(draw, "UPDATE")
        cx = ((self.MENU_X0 + self.MENU_X1) // 2) * SS
        phase = (_phase(st) or "queued").upper()
        gauges._centered_text(draw, cx, 215 * SS, phase,
                              font(54 * SS, "bold"), TEXT)
        target = (st or {}).get("target")
        if target:
            gauges._centered_text(draw, cx, 262 * SS, "-> %s" % target,
                                  font(22 * SS, "regular"), TEXT_DIM)
        # Spinner dots (one lit at a time).
        lit = int(time.monotonic() / 0.4) % 3
        for i in range(3):
            dx = cx + (i - 1) * 36 * SS
            col = ACCENT_GLOW if i == lit else mix(BG, TEXT_DIM, 0.5)
            r = 7 * SS
            draw.ellipse([dx - r, 300 * SS - r, dx + r, 300 * SS + r],
                         fill=col)
        lfont = font(19 * SS, "mono")
        y = 350
        for d in updates.status_lines(6):
            col = DANGER if d.get("ok") is False else TEXT_DIM
            draw.text(((self.MENU_X0 + 40) * SS, y * SS),
                      self._fmt_line(d), font=lfont, fill=col)
            y += 34

    def _draw_result(self, draw, st):
        self._draw_header(draw, "UPDATE")
        ph = _phase(st)
        if ph == "done":
            title, col = "UPDATE COMPLETE", OK
        elif ph.startswith("rolled-back"):
            title, col = "ROLLED BACK", DANGER
        elif ph == "busy":
            title, col = "UPDATER BUSY", WARN
        else:
            title, col = "UPDATE FAILED", DANGER
        x0 = (self.MENU_X0 + 60) * SS
        x1 = (self.MENU_X1 - 60) * SS
        draw.rounded_rectangle([x0, 230 * SS, x1, 430 * SS], radius=16 * SS,
                               fill=mix(BG, col, 0.16),
                               outline=mix(BG, col, 0.6), width=2 * SS)
        cx = ((self.MENU_X0 + self.MENU_X1) // 2) * SS
        gauges._centered_text(draw, cx, 300 * SS, title,
                              font(48 * SS, "bold"), col)
        sub = str((st or {}).get("error") or "")
        if not sub:
            cur = (st or {}).get("current") or updates.current_version()
            sub = "now on %s" % cur if cur else ""
        if sub:
            gauges._centered_text(draw, cx, 370 * SS, sub[:70],
                                  font(22 * SS, "regular"), TEXT_DIM)
        gauges._centered_text(draw, cx, 480 * SS, "TAP TO DISMISS",
                              font(17 * SS, "bold"), mix(BG, TEXT_DIM, 0.7))

    # -- input (gate on mode) ----------------------------------------------------- #
    def handle_tap(self, x, y, ctx):
        mode, st = self._mode()
        if mode == "result":
            self._dismiss_result(st)
            return True
        if mode == "progress":
            return True
        return super().handle_tap(x, y, ctx)

    def handle_hold(self, x, y, ctx):
        mode, st = self._mode()
        if mode == "result":
            self._dismiss_result(st)
            return True
        if mode == "progress":
            return False  # let the hold fallback cycle away mid-update
        return super().handle_hold(x, y, ctx)

    def handle_swipe_v(self, direction, ctx):
        mode, _ = self._mode()
        if mode != "menu":
            return False
        return super().handle_swipe_v(direction, ctx)
