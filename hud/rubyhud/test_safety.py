"""Sim-only tests for the roof/window SafetyGate + Actuator -- no hardware, no car.

Run:  /home/michael/ruby-env/bin/python -m rubyhud.test_safety
 or:  python -m pytest hud/rubyhud/test_safety.py
"""

from types import SimpleNamespace
import time

from .safety import SafetyGate
from .actuator import Actuator, Backend


def snap(**kw):
    base = dict(source="LIVE", speed_mph=0.0, gear="P", reverse=False,
                parking_brake=True, trunk=False, roof="CLOSED")
    base.update(kw)
    return SimpleNamespace(**base)


class RecordBackend(Backend):
    def __init__(self):
        self.windows = []      # (side, direction, on)
        self.roof = []         # commands sent
        self.offs = 0
    def window_set(self, side, direction, on):
        self.windows.append((side, direction, on))
    def roof_send(self, command):
        self.roof.append(command)
    def all_off(self):
        self.offs += 1


def _wait_idle(a, t=5.0):
    t0 = time.monotonic()
    while a.roof_active() and time.monotonic() - t0 < t:
        time.sleep(0.05)


# ---- SafetyGate -----------------------------------------------------------
def test_roof_allowed_when_safe():
    assert SafetyGate(lambda: snap()).roof_reason() is None


def test_roof_blocks_each_precondition():
    cases = [
        ("no live CAN data", snap(source="NO DATA")),
        ("no live CAN data", snap(source="SIM")),
        ("speed unknown", snap(speed_mph=None)),
        ("slow", snap(speed_mph=20.0)),
        ("P or N", snap(gear="D")),
        ("P or N", snap(reverse=True)),
        ("parking brake", snap(parking_brake=False)),
        ("trunk", snap(trunk=True)),
        ("roof is", snap(roof="OPENING")),
    ]
    for needle, s in cases:
        r = SafetyGate(lambda s=s: s).roof_reason()
        assert r is not None and needle in r, (needle, r)


def test_roof_allow_moving_ignores_midcycle_only():
    assert SafetyGate(lambda: snap(roof="OPENING")).roof_reason(allow_moving=True) is None
    assert SafetyGate(lambda: snap(roof="OPENING", speed_mph=20.0)).roof_reason(allow_moving=True) is not None


def test_window_allowed_and_blocked():
    assert SafetyGate(lambda: snap()).window_reason() is None
    assert SafetyGate(lambda: snap(source="NO DATA")).window_reason() is not None


def test_gate_fails_closed_on_exception():
    def boom():
        raise RuntimeError("snapshot blew up")
    assert SafetyGate(boom).roof_reason() is not None
    assert SafetyGate(boom).window_reason() is not None


# ---- Actuator -------------------------------------------------------------
def test_disarmed_is_noop():
    b = RecordBackend()
    a = Actuator(lambda: snap(), backend=b)
    assert a.window_run("driver", "up") == "disarmed"
    assert a.roof_command("open") == "disarmed"
    assert b.windows == [] and b.roof == []


def test_armed_window_runs_and_stops():
    b = RecordBackend()
    a = Actuator(lambda: snap(), backend=b); a.arm()
    assert a.window_run("driver", "up") is None
    assert ("driver", "up", True) in b.windows
    a.window_stop("driver")
    assert ("driver", "up", False) in b.windows
    assert a.window_running() == {}


def test_window_blocked_when_unsafe():
    b = RecordBackend()
    a = Actuator(lambda: snap(source="NO DATA"), backend=b); a.arm()
    assert a.window_run("driver", "up") is not None
    assert b.windows == []


def test_window_never_both_directions():
    b = RecordBackend()
    a = Actuator(lambda: snap(), backend=b); a.arm()
    a.window_run("driver", "up")
    a.window_run("driver", "down")
    assert ("driver", "up", False) in b.windows
    assert ("driver", "down", True) in b.windows
    assert a.window_running() == {"driver": "down"}


def test_watchdog_stops_window():
    b = RecordBackend()
    a = Actuator(lambda: snap(), backend=b); a.arm()
    a.window_run("driver", "up")
    a._watchdog_fire("driver")           # simulate the backstop timer firing
    assert ("driver", "up", False) in b.windows
    assert a.window_running() == {}


def test_estop_stops_everything():
    b = RecordBackend()
    a = Actuator(lambda: snap(), backend=b); a.arm()
    a.window_run("driver", "up")
    a.estop()
    assert ("driver", "up", False) in b.windows
    assert "STOP" in b.roof and b.offs >= 1


def test_roof_starts_and_completes():
    st = dict(source="LIVE", speed_mph=0.0, gear="P", reverse=False,
              parking_brake=True, trunk=False, roof="CLOSED")
    b = RecordBackend()
    a = Actuator(lambda: SimpleNamespace(**st), backend=b); a.arm()
    assert a.roof_command("open") is None
    assert "GO open" in b.roof
    st["roof"] = "OPENING"; time.sleep(0.5)
    st["roof"] = "OPEN"; time.sleep(0.5)
    _wait_idle(a)
    assert not a.roof_active()
    assert b.roof[-1] == "STOP"


def test_roof_aborts_on_speed_midcycle():
    st = dict(source="LIVE", speed_mph=0.0, gear="P", reverse=False,
              parking_brake=True, trunk=False, roof="CLOSED")
    b = RecordBackend()
    a = Actuator(lambda: SimpleNamespace(**st), backend=b); a.arm()
    a.roof_command("open")
    st["roof"] = "OPENING"; time.sleep(0.4)
    st["speed_mph"] = 30.0               # car rolls away mid-cycle
    _wait_idle(a)
    assert not a.roof_active()
    assert b.roof[-1] == "STOP"


def test_roof_busy_rejects_second():
    st = dict(source="LIVE", speed_mph=0.0, gear="P", reverse=False,
              parking_brake=True, trunk=False, roof="CLOSED")
    b = RecordBackend()
    a = Actuator(lambda: SimpleNamespace(**st), backend=b); a.arm()
    a.roof_command("open")
    # roof status still CLOSED (gate passes) but a cycle thread is active -> the
    # "roof busy" guard rejects a second command. Once OPENING, the gate's own
    # mid-cycle check would reject it instead (defense in depth).
    assert a.roof_command("close") == "roof busy"
    a.roof_stop(); _wait_idle(a)


def _run():
    import traceback
    fns = sorted((k, v) for k, v in globals().items()
                 if k.startswith("test_") and callable(v))
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS  " + name); passed += 1
        except Exception:
            print("FAIL  " + name); traceback.print_exc(); failed += 1
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run())
