#!/usr/bin/env python3
"""Sniff the window LIN bus through a UART-LIN transceiver on a Pi serial port.

Logs timestamped byte groups (split on idle gaps ~ frame boundaries). Capture a
baseline, then operate ONE passenger-window action at a time (up / down / auto-up
/ auto-down) and diff: byte1 = switch states, byte2 = the up/down/auto payload to
the passenger motor controller. Use the slowest baud that frames cleanly -- try
19200 first, then 9600 / 10417.

  ./lin-capture.py /dev/serial0 19200 > logs/lin-baseline.txt
  ./lin-capture.py /dev/serial0 19200 > logs/lin-pass-up.txt

SAFETY: 12 V on a Pi UART destroys the Pi. ONLY run this through a LIN transceiver
(TJA1021 / MCP2003B) with 3.3 V level protection, verified with a meter BEFORE
powering the Pi. The window LIN is the master->passenger line (driver-switch MCU
is the master); replaying onto it is research-only -- prefer paralleling each
window switch's own logic contacts. See mx5-roof-window-can.
"""

import sys
import time

try:
    import serial
except ImportError:
    sys.exit("needs pyserial:  /home/michael/ruby-env/bin/pip install pyserial")

GAP_S = 0.020   # idle gap that marks a frame boundary (tune per baud)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/serial0"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    ser = serial.Serial(port, baud, timeout=0.01)
    sys.stderr.write("LIN sniff %s @ %d baud -- Ctrl-C to stop\n" % (port, baud))
    sys.stderr.flush()
    buf = bytearray()
    last = time.monotonic()
    try:
        while True:
            chunk = ser.read(64)
            now = time.monotonic()
            if chunk:
                buf.extend(chunk)
                last = now
            elif buf and (now - last) > GAP_S:
                print("(%.4f) %s" % (now, " ".join("%02X" % x for x in buf)))
                sys.stdout.flush()
                buf.clear()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")


if __name__ == "__main__":
    main()
