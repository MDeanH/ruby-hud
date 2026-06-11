"""rubyhud SIM driver.

Standalone script. Opens vcan0 and emits the SIM-ID frame set (defined in
signals.py) at ~50 Hz total, animating a continuous driving cycle:

    idle (800 rpm, 0 mph) -> accelerate through gears (rpm rises to ~6500
    then shift up, speed climbs) -> cruise -> decelerate -> repeat.

Coolant warms 40 -> 92 then holds. Volts ~14.1 with small noise. Throttle
tracks acceleration. Fuel slowly decreases.

All motion is time-based; the only "noise" comes from a deterministic
counter-based LFSR (no random-number primitive is ever called).
"""

from __future__ import annotations

import signal
import sys
import time

import can

from rubyhud import signals as S


# --------------------------------------------------------------------------- #
# Deterministic noise: 16-bit Galois LFSR advanced by a counter (no RNG).
# --------------------------------------------------------------------------- #
def _lfsr(counter: int) -> int:
    """Return a deterministic pseudo-value in 0..65535 for a given counter."""
    state = (counter * 2654435761) & 0xFFFF
    if state == 0:
        state = 0xACE1
    for _ in range(8):
        lsb = state & 1
        state >>= 1
        if lsb:
            state ^= 0xB400
    return state & 0xFFFF


def _noise(counter: int, amplitude: float) -> float:
    """Signed deterministic noise in roughly [-amplitude, +amplitude]."""
    return ((_lfsr(counter) / 65535.0) - 0.5) * 2.0 * amplitude


# --------------------------------------------------------------------------- #
# Driving-cycle model (pure function of elapsed time t, in seconds).
# Cycle length 60s: idle -> accel -> cruise -> decel -> idle.
# --------------------------------------------------------------------------- #
_IDLE_RPM = 800.0
_SHIFT_RPM = 6500.0
_GEAR_TOP_SPEED = {1: 12.0, 2: 28.0, 3: 48.0, 4: 72.0, 5: 95.0, 6: 120.0}
_GEAR_ORDER = [1, 2, 3, 4, 5, 6]


def _phase(t: float) -> tuple[float, float, str, float]:
    """Return (speed_mph, rpm, gear_str, throttle_pct) for cycle-time t."""
    cycle = t % 60.0

    if cycle < 6.0:
        # idle
        return 0.0, _IDLE_RPM, "N", 0.0

    if cycle < 30.0:
        # accelerate: 24s ramp from 0 to top cruise speed (~100 mph)
        frac = (cycle - 6.0) / 24.0
        speed = 100.0 * frac
        throttle = 85.0
    elif cycle < 44.0:
        # cruise at ~100 mph (above gear-5 top so gear 6 is selected and the
        # HUD shows a relaxed top-gear rpm instead of redline in 5th)
        speed = 100.0
        throttle = 28.0
    else:
        # decelerate over 16s back toward 0
        frac = (cycle - 44.0) / 16.0
        speed = 100.0 * (1.0 - frac)
        throttle = 0.0

    if speed < 0.0:
        speed = 0.0

    # Pick gear from speed; rpm scales within the gear's band.
    gear = 1
    for g in _GEAR_ORDER:
        gear = g
        if speed <= _GEAR_TOP_SPEED[g]:
            break

    lo_speed = 0.0 if gear == 1 else _GEAR_TOP_SPEED[gear - 1]
    hi_speed = _GEAR_TOP_SPEED[gear]
    span = max(hi_speed - lo_speed, 0.1)
    band = (speed - lo_speed) / span
    if band < 0.0:
        band = 0.0
    if band > 1.0:
        band = 1.0
    rpm = 1500.0 + band * (_SHIFT_RPM - 1500.0)

    if speed < 0.5:
        return 0.0, _IDLE_RPM, "N", 0.0
    return speed, rpm, str(gear), throttle


def _coolant(elapsed: float) -> float:
    """Warm 40 -> 92 over ~120s, then hold."""
    target = 92.0
    start = 40.0
    warm_secs = 120.0
    if elapsed >= warm_secs:
        return target
    return start + (target - start) * (elapsed / warm_secs)


def _fuel(elapsed: float) -> float:
    """Slowly decrease from 100% over time (about 1% per 90s)."""
    pct = 100.0 - elapsed / 90.0
    if pct < 0.0:
        pct = 0.0
    return pct


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
_RUNNING = True


def _on_sigterm(_signo, _frame):
    global _RUNNING
    _RUNNING = False


def main() -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    bus = can.interface.Bus("vcan0", interface="socketcan")

    period = 1.0 / 50.0  # ~50 Hz total send cadence
    counter = 0
    start = time.monotonic()
    next_tick = start

    try:
        while _RUNNING:
            now = time.monotonic()
            elapsed = now - start

            speed, rpm, gear, throttle = _phase(elapsed)
            coolant = _coolant(elapsed)
            fuel = _fuel(elapsed)
            volts = 14.1 + _noise(counter, 0.15)
            rpm_n = rpm + _noise(counter + 7, 40.0)
            if rpm_n < 0.0:
                rpm_n = 0.0

            frames = (
                can.Message(arbitration_id=S.ID_RPM,
                            data=S.encode_rpm(rpm_n), is_extended_id=False),
                can.Message(arbitration_id=S.ID_SPEED,
                            data=S.encode_speed(speed), is_extended_id=False),
                can.Message(arbitration_id=S.ID_TEMP,
                            data=S.encode_temp(coolant), is_extended_id=False),
                can.Message(arbitration_id=S.ID_AUX,
                            data=S.encode_aux(volts, throttle, gear, fuel),
                            is_extended_id=False),
            )
            for msg in frames:
                try:
                    bus.send(msg)
                except Exception:
                    pass

            counter += 1
            next_tick += period
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # fell behind; resync to avoid busy spin
                next_tick = time.monotonic()
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
