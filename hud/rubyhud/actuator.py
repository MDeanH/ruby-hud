"""Disarmed-by-default actuator: the ONLY thing in rubyhud that may transmit to
the roof bridge or the window relays.

Constructed DISARMED -- every actuation is a no-op until arm() is called (the
software analogue of the listen-only CAN guard). A pluggable Backend abstracts
the hardware; the default LogBackend is a dry-run that only logs, so the whole
state machine + SafetyGate run in sim with no wiring. The real GpioBackend
(relays + bridge serial) slots in later without touching this logic.

Windows are hold-to-run with THREE independent stops: the caller's release, a
per-window watchdog (WINDOW_MAX_RUN_S), and the backend's enable line (which the
real GPIO backend MUST default OFF at boot). Opposite directions can never run
at once. The roof is gated, driven via the bridge, and watched closed-loop:
roof_command() sends GO and runs a monitor that re-checks the SafetyGate every
tick (continuous speed/gear abort -> restore interlock) and watches roof status
(0x472) for completion/stall -> STOP. Nothing here puts a frame on can0.
"""

from __future__ import annotations

import atexit
import threading
import time

from .safety import SafetyGate, _ROOF_MOVING

WINDOW_MAX_RUN_S = 6.0         # hard cap on any single window run, UI or not
ROOF_CYCLE_TIMEOUT = 16.0      # ~13s typical RF stow; stall guard above it
ROOF_POLL_S = 0.2

_SIDES = ("driver", "passenger")
_DIRS = ("up", "down")
_ROOF_DIRS = ("open", "close")


class Backend:
    """Hardware interface. Every method must be safe when disarmed/duplicated and
    must never raise. Implementations: LogBackend (dry-run) and, later, a
    GpioBackend (window relays + the Pi->bridge serial/CAN link)."""

    def window_set(self, side: str, direction: str, on: bool) -> None:
        ...

    def roof_send(self, command: str) -> None:        # "GO open"|"GO close"|"STOP"
        ...

    def all_off(self) -> None:                        # de-energize everything
        ...


class LogBackend(Backend):
    """Dry-run backend: logs intent, touches no hardware. Used in sim/tests."""

    def __init__(self, log=None):
        self._log = log or (lambda m: None)

    def window_set(self, side, direction, on):
        self._log("window %s %s -> %s" % (side, direction, "ON" if on else "off"))

    def roof_send(self, command):
        self._log("bridge <- %s" % command)

    def all_off(self):
        self._log("ALL OFF (de-energize)")


class Actuator:
    def __init__(self, snapshot_fn, backend: "Backend | None" = None, log=None):
        self._snapshot = snapshot_fn
        self.gate = SafetyGate(snapshot_fn)
        self._backend = backend or LogBackend(log)
        self._log_fn = log or (lambda m: None)
        self._lock = threading.RLock()
        self._armed = False
        self._win_timers = {}        # side -> threading.Timer
        self._win_on = {}            # side -> direction currently running
        self._roof_thread = None
        self._roof_stop = threading.Event()
        atexit.register(self.close)

    def _log(self, m):
        try:
            self._log_fn("actuator: %s" % m)
        except Exception:
            pass

    # -- arming --------------------------------------------------------------- #
    def arm(self):
        with self._lock:
            self._armed = True
        self._log("ARMED")

    def disarm(self):
        self.estop()
        with self._lock:
            self._armed = False
        self._log("DISARMED")

    def armed(self) -> bool:
        return self._armed

    # -- windows (hold-to-run) ------------------------------------------------ #
    def window_run(self, side: str, direction: str):
        """Begin/continue moving a window. Returns None on success, else a short
        refusal reason (and is a no-op). The caller MUST call window_stop on
        finger-release; the watchdog is only a backstop."""
        if side not in _SIDES or direction not in _DIRS:
            return "bad window"
        if not self._armed:
            return "disarmed"
        why = self.gate.window_reason()
        if why:
            return why
        with self._lock:
            if self._win_on.get(side) not in (None, direction):
                self._window_off_locked(side)        # never both directions
            if self._win_on.get(side) != direction:
                self._win_on[side] = direction
                self._backend.window_set(side, direction, True)
            self._arm_watchdog_locked(side)
        return None

    def window_stop(self, side: str = None):
        with self._lock:
            for s in ([side] if side else list(self._win_on.keys())):
                self._window_off_locked(s)

    def _window_off_locked(self, side):
        t = self._win_timers.pop(side, None)
        if t:
            t.cancel()
        d = self._win_on.pop(side, None)
        if d is not None:
            self._backend.window_set(side, d, False)

    def _arm_watchdog_locked(self, side):
        t = self._win_timers.pop(side, None)
        if t:
            t.cancel()
        wt = threading.Timer(WINDOW_MAX_RUN_S, self._watchdog_fire, args=(side,))
        wt.daemon = True
        self._win_timers[side] = wt
        wt.start()

    def _watchdog_fire(self, side):
        self._log("watchdog stop: %s held > %ds" % (side, int(WINDOW_MAX_RUN_S)))
        with self._lock:
            self._window_off_locked(side)

    def window_running(self):
        with self._lock:
            return dict(self._win_on)

    # -- roof (gated + bridge + closed-loop monitor) -------------------------- #
    def roof_command(self, direction: str):
        """Start a roof cycle via the bridge. Returns None on success, else the
        refusal reason. The UI should require a two-tap confirm before calling."""
        if direction not in _ROOF_DIRS:
            return "bad roof dir"
        if not self._armed:
            return "disarmed"
        why = self.gate.roof_reason()
        if why:
            return why
        with self._lock:
            if self._roof_thread is not None:
                return "roof busy"
            self._roof_stop.clear()
            self._backend.roof_send("GO %s" % direction)
            self._roof_thread = threading.Thread(
                target=self._roof_monitor, args=(direction,), daemon=True)
            self._roof_thread.start()
        self._log("roof %s started" % direction)
        return None

    def roof_stop(self):
        self._roof_stop.set()

    def _roof_monitor(self, direction):
        start = time.monotonic()
        saw_moving = False
        reason = "stopped"
        while not self._roof_stop.wait(ROOF_POLL_S):
            why = self.gate.roof_reason(allow_moving=True)   # continuous abort
            if why:
                reason = "abort (%s)" % why
                break
            snap = None
            try:
                snap = self._snapshot()
            except Exception:
                pass
            roof = getattr(snap, "roof", "-") if snap else "-"
            if roof in _ROOF_MOVING:
                saw_moving = True
            elif saw_moving and roof in ("OPEN", "CLOSED"):
                reason = "complete (%s)" % roof
                break
            if time.monotonic() - start > ROOF_CYCLE_TIMEOUT:
                reason = "timeout/stall"
                break
        self._backend.roof_send("STOP")          # always release the bridge
        self._log("roof %s: %s" % (direction, reason))
        with self._lock:
            self._roof_thread = None

    def roof_active(self) -> bool:
        return self._roof_thread is not None

    # -- panic / shutdown ----------------------------------------------------- #
    def estop(self):
        self._roof_stop.set()
        with self._lock:
            for s in list(self._win_on.keys()):
                self._window_off_locked(s)
        try:
            self._backend.roof_send("STOP")
            self._backend.all_off()
        except Exception:
            pass
        self._log("ESTOP")

    def close(self):
        try:
            self.estop()
        except Exception:
            pass
