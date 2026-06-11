#!/usr/bin/env python3
"""touch-test.py -- inject synthetic touch gestures for rubyhud testing.

Creates a virtual touchscreen device ('rubyhud-test-touch') via evdev.UInput
and replays gestures given on the command line. Coordinates are screen pixels
on the 1280x800 panel; they are scaled to the 0..4095 ABS range. Must run as
root (uinput access). This tool only injects input events; it never touches
the CAN bus.

Usage:
    sudo python3 touch-test.py tap 640 400 sleep 1 swipe-left hold 100 700

Commands (parsed left to right from argv):
    tap X Y       press + release at pixel (X, Y)
    hold X Y      press, hold 0.6 s, release at pixel (X, Y)
    swipe-left    horizontal drag, x 900 -> 300 at y 400
    swipe-right   horizontal drag, x 300 -> 900 at y 400
    sleep N       pause N seconds
"""

import sys
import time

from evdev import AbsInfo, UInput, ecodes as e

DEVICE_NAME = "rubyhud-test-touch"
SCREEN_W = 1280
SCREEN_H = 800
ABS_MAX = 4095

TAP_DOWN_SECS = 0.05
HOLD_SECS = 0.6
SWIPE_STEPS = 12
SWIPE_SECS = 0.25
GESTURE_GAP_SECS = 0.8
# Must cover the HUD's device discovery: TouchInput rescans every RESCAN_S
# (1.0s, touch.py) and its select() blocks up to 1.0s between gate checks,
# so worst-case pickup is ~2.0s. Events injected before a reader opens the
# node are NOT replayed, so injecting earlier silently loses gestures.
DEVICE_SETTLE_SECS = 2.5

# Resolution hints (units per mm) for a ~150x94 mm 7-inch panel.
CAPABILITIES = {
    e.EV_KEY: [e.BTN_TOUCH],
    e.EV_ABS: [
        (e.ABS_X, AbsInfo(value=0, min=0, max=ABS_MAX,
                          fuzz=0, flat=0, resolution=27)),
        (e.ABS_Y, AbsInfo(value=0, min=0, max=ABS_MAX,
                          fuzz=0, flat=0, resolution=44)),
    ],
}


def px_to_abs_x(px):
    px = max(0, min(SCREEN_W - 1, int(px)))
    return px * ABS_MAX // (SCREEN_W - 1)


def px_to_abs_y(px):
    px = max(0, min(SCREEN_H - 1, int(px)))
    return px * ABS_MAX // (SCREEN_H - 1)


def move(ui, x_px, y_px):
    ui.write(e.EV_ABS, e.ABS_X, px_to_abs_x(x_px))
    ui.write(e.EV_ABS, e.ABS_Y, px_to_abs_y(y_px))
    ui.syn()


def press(ui, x_px, y_px):
    ui.write(e.EV_ABS, e.ABS_X, px_to_abs_x(x_px))
    ui.write(e.EV_ABS, e.ABS_Y, px_to_abs_y(y_px))
    ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
    ui.syn()


def release(ui):
    ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
    ui.syn()


def do_tap(ui, x, y):
    press(ui, x, y)
    time.sleep(TAP_DOWN_SECS)
    release(ui)
    print("tap %d %d" % (x, y))


def do_hold(ui, x, y):
    press(ui, x, y)
    time.sleep(HOLD_SECS)
    release(ui)
    print("hold %d %d" % (x, y))


def do_swipe(ui, x_from, x_to, y, label):
    press(ui, x_from, y)
    step_sleep = SWIPE_SECS / SWIPE_STEPS
    for i in range(1, SWIPE_STEPS + 1):
        frac = float(i) / SWIPE_STEPS
        x = x_from + (x_to - x_from) * frac
        move(ui, int(round(x)), y)
        time.sleep(step_sleep)
    release(ui)
    print(label)


def parse_commands(argv):
    """Parse argv left-to-right into a command list, or raise ValueError."""
    cmds = []
    i = 0
    while i < len(argv):
        word = argv[i]
        if word in ("tap", "hold"):
            if i + 2 >= len(argv):
                raise ValueError("%s needs X Y" % word)
            cmds.append((word, int(argv[i + 1]), int(argv[i + 2])))
            i += 3
        elif word in ("swipe-left", "swipe-right"):
            cmds.append((word,))
            i += 1
        elif word == "sleep":
            if i + 1 >= len(argv):
                raise ValueError("sleep needs N")
            cmds.append((word, float(argv[i + 1])))
            i += 2
        else:
            raise ValueError("unknown command %r" % word)
    return cmds


def main(argv):
    try:
        cmds = parse_commands(argv)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        sys.stderr.write(__doc__)
        return 2
    if not cmds:
        sys.stderr.write(__doc__)
        return 2

    ui = UInput(CAPABILITIES, name=DEVICE_NAME)
    try:
        # Give udev / the HUD's input listener time to pick up the device.
        time.sleep(DEVICE_SETTLE_SECS)
        first = True
        for cmd in cmds:
            if cmd[0] == "sleep":
                time.sleep(cmd[1])
                print("sleep %g" % cmd[1])
                continue
            if not first:
                time.sleep(GESTURE_GAP_SECS)
            first = False
            if cmd[0] == "tap":
                do_tap(ui, cmd[1], cmd[2])
            elif cmd[0] == "hold":
                do_hold(ui, cmd[1], cmd[2])
            elif cmd[0] == "swipe-left":
                do_swipe(ui, 900, 300, 400, "swipe-left")
            else:
                do_swipe(ui, 300, 900, 400, "swipe-right")
    finally:
        ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
