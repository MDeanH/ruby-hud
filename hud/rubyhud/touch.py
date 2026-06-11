"""Touchscreen input for rubyhud.

evdev-backed gesture reader with zero hard dependency: `import evdev` happens
inside start(); when it is missing, .available stays False and every public
method no-ops, so the HUD runs unchanged on hosts without python3-evdev.

Reads every evdev device that looks like a touchscreen (multitouch
ABS_MT_POSITION_X/Y preferred, plain ABS_X/Y + BTN_TOUCH accepted), tracks
only the primary contact per device, and synthesizes gestures on contact UP:

  ('tap', x, y)          duration < 0.45s and total movement < 45 px
  ('hold', x, y)         duration >= 0.45s and total movement < 45 px
  ('swipe_left', x, y)   dx < -130 px and |dx| > 2|dy| (any duration)
  ('swipe_right', x, y)  dx > +130 px and |dx| > 2|dy| (any duration)

x/y are screen pixels at the contact-DOWN point, normalized from each
device's absinfo range to the (w, h) given to the constructor. Devices are
rescanned every 1s so hot-plugged (and virtual uinput test) touchscreens are
picked up while running. The reader thread never dies: all errors are caught
and logged (throttled to <= 1 line/s) to /tmp/rubyhud.log.
"""

from __future__ import annotations

import collections
import select
import threading
import time

_LOG = "/tmp/rubyhud.log"

TAP_MAX_S = 0.45
MOVE_MAX_PX = 45.0
SWIPE_MIN_PX = 130.0
# Device rescan period. Kept short: the scan only opens *unknown* /dev/input
# paths (cheap), and events written to an evdev node before a reader opens it
# are never replayed -- a long period silently eats early gestures from
# hot-plugged (and uinput test) devices. deploy/touch-test.py's settle wait
# assumes discovery within RESCAN_S + the 1.0s select timeout in _loop_once.
RESCAN_S = 1.0


class _Contact:
    """Primary-contact state machine for one device. Events accumulate as
    pend_* flags + raw coords and are committed on SYN_REPORT."""

    __slots__ = ("down", "pend_down", "pend_up", "raw_x", "raw_y", "slot",
                 "down_t", "down_px", "last_px", "max_move")

    def __init__(self):
        self.down = False
        self.pend_down = False
        self.pend_up = False
        self.raw_x = None
        self.raw_y = None
        self.slot = 0          # current MT slot (we only track slot 0)
        self.down_t = 0.0
        self.down_px = (0, 0)
        self.last_px = (0, 0)
        self.max_move = 0.0


class TouchInput:
    def __init__(self, w: int = 1280, h: int = 800):
        self.w = max(1, int(w))
        self.h = max(1, int(h))
        self.available = False
        self.device_names: list[str] = []
        self._evdev = None
        self._e = None  # evdev.ecodes
        self._devices: dict = {}  # fd -> entry dict
        self._events: collections.deque = collections.deque(maxlen=32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log = 0.0

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import evdev  # soft dependency: degrade silently when missing
        except Exception:
            self.available = False
            self._log("touch: evdev unavailable; touch input disabled")
            return
        self._evdev = evdev
        self._e = evdev.ecodes
        self.available = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="rubyhud-touch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)
        self._thread = None
        for fd in list(self._devices):
            self._drop(fd, quiet=True)

    def events(self) -> list:
        """Drain and return pending gesture tuples (kind, x, y)."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    # -- logging (throttled) ------------------------------------------------ #
    def _log(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_log < 1.0:
            return
        self._last_log = now
        try:
            with open(_LOG, "a") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    # -- reader thread ------------------------------------------------------ #
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop_once()
            except Exception as exc:
                self._log("touch loop error: %r" % (exc,))
                self._stop.wait(0.5)

    def _loop_once(self) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_scan", 0.0) >= RESCAN_S:
            self._last_scan = now
            self._scan()
        if not self._devices:
            self._stop.wait(0.5)
            return
        fds = list(self._devices.keys())
        try:
            readable, _, _ = select.select(fds, [], [], 1.0)
        except Exception:
            # A device vanished between scans; drop everything stale.
            for fd in fds:
                try:
                    select.select([fd], [], [], 0)
                except Exception:
                    self._drop(fd)
            return
        for fd in readable:
            entry = self._devices.get(fd)
            if entry is None:
                continue
            try:
                for ev in entry["dev"].read():
                    self._handle_event(entry, ev)
            except OSError:
                self._drop(fd)  # unplugged; rescan will find replacements
            except Exception as exc:
                self._log("touch read error: %r" % (exc,))

    def _scan(self) -> None:
        e = self._e
        known = {entry["path"] for entry in self._devices.values()}
        try:
            paths = self._evdev.list_devices()
        except Exception as exc:
            self._log("touch scan error: %r" % (exc,))
            return
        for path in paths:
            if path in known:
                continue
            dev = None
            try:
                dev = self._evdev.InputDevice(path)
                caps = dev.capabilities()
                abs_caps = dict(caps.get(e.EV_ABS, []))
                keys = set(caps.get(e.EV_KEY, []))
                mt = (e.ABS_MT_POSITION_X in abs_caps
                      and e.ABS_MT_POSITION_Y in abs_caps)
                st = (e.ABS_X in abs_caps and e.ABS_Y in abs_caps
                      and e.BTN_TOUCH in keys)
                if not mt and not st:
                    dev.close()
                    continue
                ax = abs_caps[e.ABS_MT_POSITION_X if mt else e.ABS_X]
                ay = abs_caps[e.ABS_MT_POSITION_Y if mt else e.ABS_Y]
                c = _Contact()
                # Seed coords from the device's current ABS state: the kernel
                # suppresses ABS events whose value equals that state, so the
                # first contact at exactly this coordinate would otherwise
                # never deliver a position event and the DOWN would be lost.
                c.raw_x = ax.value
                c.raw_y = ay.value
                self._devices[dev.fd] = {
                    "dev": dev, "path": path, "mt": mt,
                    "xmin": ax.min, "xmax": ax.max,
                    "ymin": ay.min, "ymax": ay.max,
                    "c": c,
                }
                with self._lock:
                    self.device_names.append(dev.name)
                self._log("touch: using %s (%s, %s)"
                          % (dev.name, path, "MT" if mt else "ST"))
            except Exception:
                try:
                    if dev is not None:
                        dev.close()
                except Exception:
                    pass

    def _drop(self, fd, quiet: bool = False) -> None:
        entry = self._devices.pop(fd, None)
        if entry is None:
            return
        try:
            entry["dev"].close()
        except Exception:
            pass
        with self._lock:
            try:
                self.device_names.remove(entry["dev"].name)
            except Exception:
                pass
        if not quiet:
            self._log("touch: lost %s" % entry["path"])

    # -- event parsing ------------------------------------------------------ #
    def _handle_event(self, entry, ev) -> None:
        e = self._e
        c = entry["c"]
        if ev.type == e.EV_ABS:
            if entry["mt"]:
                if ev.code == e.ABS_MT_SLOT:
                    c.slot = ev.value
                elif c.slot != 0:
                    return  # only the primary contact
                elif ev.code == e.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        c.pend_up = True
                    else:
                        c.pend_down = True
                elif ev.code == e.ABS_MT_POSITION_X:
                    c.raw_x = ev.value
                elif ev.code == e.ABS_MT_POSITION_Y:
                    c.raw_y = ev.value
            else:
                if ev.code == e.ABS_X:
                    c.raw_x = ev.value
                elif ev.code == e.ABS_Y:
                    c.raw_y = ev.value
        elif (ev.type == e.EV_KEY and ev.code == e.BTN_TOUCH
              and not entry["mt"]):
            if ev.value:
                c.pend_down = True
            else:
                c.pend_up = True
        elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
            self._commit(entry)

    def _to_px(self, entry, c):
        if c.raw_x is None or c.raw_y is None:
            return None
        xs = entry["xmax"] - entry["xmin"]
        ys = entry["ymax"] - entry["ymin"]
        if xs <= 0 or ys <= 0:
            return None
        x = (c.raw_x - entry["xmin"]) / xs * (self.w - 1)
        y = (c.raw_y - entry["ymin"]) / ys * (self.h - 1)
        return (max(0.0, min(self.w - 1.0, x)),
                max(0.0, min(self.h - 1.0, y)))

    def _commit(self, entry) -> None:
        c = entry["c"]
        now = time.monotonic()
        px = self._to_px(entry, c)
        if c.pend_down and not c.down and px is not None:
            c.pend_down = False
            c.down = True
            c.down_t = now
            c.down_px = px
            c.last_px = px
            c.max_move = 0.0
        elif c.down and px is not None:
            c.last_px = px
            dx = px[0] - c.down_px[0]
            dy = px[1] - c.down_px[1]
            mv = (dx * dx + dy * dy) ** 0.5
            if mv > c.max_move:
                c.max_move = mv
        if c.pend_up:
            c.pend_up = False
            c.pend_down = False
            if c.down:
                c.down = False
                gesture = self._classify(c, now)
                if gesture is not None:
                    with self._lock:
                        self._events.append(gesture)

    @staticmethod
    def _classify(c, now):
        dur = now - c.down_t
        dx = c.last_px[0] - c.down_px[0]
        dy = c.last_px[1] - c.down_px[1]
        if abs(dx) > SWIPE_MIN_PX and abs(dx) > 2.0 * abs(dy):
            kind = "swipe_left" if dx < 0 else "swipe_right"
            return (kind, int(c.down_px[0]), int(c.down_px[1]))
        if c.max_move < MOVE_MAX_PX:
            kind = "tap" if dur < TAP_MAX_S else "hold"
            return (kind, int(c.down_px[0]), int(c.down_px[1]))
        return None
