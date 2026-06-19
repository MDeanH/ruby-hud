"""Fail-closed safety gate for roof / window actuation.

Every actuation re-reads a FRESH live Snapshot and asks the gate for permission.
The gate returns None (clear) or a short human-readable reason to REFUSE, which
the UI flashes. It fails CLOSED: anything missing, stale, non-LIVE, or any
precondition unmet -> blocked. Roof and windows have different (overlapping)
preconditions; the roof's are strict (it's a heavy hardtop that stows into the
trunk). See the mx5-roof-window-can research + its safety review.

The gate only READS a snapshot -- it never actuates. It is also re-checked
CONTINUOUSLY during a roof cycle by the Actuator (a once-at-start check would let
the car reach speed mid-deploy), so roof_reason() is cheap and side-effect-free.

Snapshot fields used: source ('LIVE'|'SIM'|'NO DATA'), speed_mph (float|None),
gear ('N'|'P'|'R'|'1'..'6'|'D'|'-'), reverse (bool), parking_brake (bool),
trunk (bool, open), roof ('CLOSED'|'OPENING'|'OPEN'|'CLOSING'|'-').
"""

from __future__ import annotations

# Mazda limits the RF roof to ~6 mph; we match that exactly (NOT higher).
ROOF_SPEED_MAX_MPH = 6.0
# Windows may be operated at any speed; we don't speed-gate them (anti-pinch +
# hold-to-run are their safety). A sanity ceiling guards only against a wild
# glitch (e.g. a stuck remote at highway speed); None disables it.
WINDOW_SPEED_MAX_MPH: float | None = None

_SAFE_GEARS = frozenset({"N", "P"})        # must be explicitly Park or Neutral
_ROOF_MOVING = frozenset({"OPENING", "CLOSING"})


class SafetyGate:
    """Holds a snapshot provider and answers window_reason()/roof_reason().

    snapshot_fn: a zero-arg callable returning a fresh Snapshot (e.g.
    DataLayer.snapshot). It is called ANEW on every check -- never cache.
    """

    def __init__(self, snapshot_fn):
        self._snapshot_fn = snapshot_fn

    def _fresh(self):
        try:
            return self._snapshot_fn()
        except Exception:
            return None        # fail closed: no readable data == no go

    @staticmethod
    def _live_reason(snap) -> str | None:
        if snap is None:
            return "no data"
        if getattr(snap, "source", None) != "LIVE":
            return "no live CAN data"
        if getattr(snap, "speed_mph", None) is None:
            return "speed unknown"
        return None

    def window_reason(self) -> str | None:
        """None if a window may move now, else why not. Windows are permissive
        (the factory anti-pinch + hold-to-run carry the safety); we only require
        fresh live data and an optional glitch-guard speed ceiling."""
        snap = self._fresh()
        why = self._live_reason(snap)
        if why:
            return why
        if (WINDOW_SPEED_MAX_MPH is not None
                and snap.speed_mph > WINDOW_SPEED_MAX_MPH):
            return "too fast (%d mph)" % round(snap.speed_mph)
        return None

    def roof_reason(self, allow_moving: bool = False) -> str | None:
        """None if the roof may move now, else why not. Strict: stationary, in
        P/N with the brake set, trunk closed, not already mid-cycle.

        allow_moving=True drops ONLY the not-mid-cycle check -- used for the
        Actuator's continuous re-check DURING a cycle (the roof is expected to be
        OPENING/CLOSING then, but speed/gear/reverse/trunk/data must still hold,
        so motion aborts and the interlock is restored if any of those slip)."""
        snap = self._fresh()
        why = self._live_reason(snap)
        if why:
            return why
        if snap.speed_mph > ROOF_SPEED_MAX_MPH:
            return "slow to %d mph (now %d)" % (
                int(ROOF_SPEED_MAX_MPH), round(snap.speed_mph))
        if getattr(snap, "reverse", False) or getattr(snap, "gear", "-") not in _SAFE_GEARS:
            return "shift to P or N"
        if not getattr(snap, "parking_brake", False):
            return "set the parking brake"
        if getattr(snap, "trunk", False):
            return "close the trunk"
        if not allow_moving and getattr(snap, "roof", "-") in _ROOF_MOVING:
            return "roof is %s" % snap.roof.lower()
        return None
