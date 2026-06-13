"""SerialStateLink -- USB-CDC NDJSON link to the Qualia satellite display.

The Qualia's USB-C can plug straight into the Ruby Pi: the ESP32-S3 native
USB CDC-ACM port then carries the exact same newline-delimited JSON protocol
as the TCP channel (STATE lines out, CMD lines in), so the satellite works
with no Wi-Fi at all. The publish loop drives this object:

    pump()        every tick: open/rescan the device, drain inbound bytes
    broadcast(l)  after each STATE build: write the line (best-effort)
    commands()    drain parsed inbound CMD dicts

Stdlib-only (termios raw tty; NO pyserial) to honor rubysat's no-deps rule.

Device discovery, in order:
  1. RUBYSAT_SERIAL_DEV env var or an explicit constructor `device` path,
  2. /dev/serial/by-id entries that look like an Espressif/Adafruit CDC port,
  3. a LONE /dev/ttyACM* (never guesses when several ACM devices exist --
     grabbing a stranger's modem/adapter would be worse than no USB link).

Hotplug-safe: a vanished device (EIO/ENXIO/EOF) closes the fd and the next
pump() rescans (throttled to every RESCAN_S). Writes are buffered so a
partial write never tears a line's framing; if the device stops draining,
the buffer is capped and reset (the firmware tolerates a torn line). Inbound
non-JSON lines (e.g. the firmware's "[rubysat] boot" debug prints) are
ignored silently. Nothing here raises out of a public method.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import termios
import time
import tty

_LOG = "/tmp/rubysat.log"

RESCAN_S = 2.0          # how often pump() looks for a device while closed
_MAX_LINE = 4096        # inbound line cap (junk guard)
_MAX_TXBUF = 8192       # outbound backlog cap before reset
_MAX_CMDS = 64          # parsed-but-undrained inbound command cap

# by-id globs that identify the Qualia's CDC port. Espressif's native USB
# stack reports the Espressif VID by default; some builds carry Adafruit or
# board branding instead.
_BY_ID_PATTERNS = (
    "/dev/serial/by-id/*Espressif*",
    "/dev/serial/by-id/*Adafruit*",
    "/dev/serial/by-id/*Qualia*",
)
_ACM_PATTERN = "/dev/ttyACM*"


class SerialStateLink:
    def __init__(self, device: str | None = None):
        self.device = device or os.environ.get("RUBYSAT_SERIAL_DEV") or None
        self._fd: int | None = None
        self._path: str | None = None
        self._rxbuf = b""
        self._txbuf = b""
        self._cmds: collections.deque = collections.deque(maxlen=_MAX_CMDS)
        self._last_scan = 0.0
        self._last_log = 0.0

    # -- logging (throttled; never raises) ---------------------------------- #
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

    # -- device discovery ---------------------------------------------------- #
    def _candidates(self) -> list:
        if self.device:
            return [self.device]
        out: list = []
        for pat in _BY_ID_PATTERNS:
            try:
                out.extend(sorted(glob.glob(pat)))
            except Exception:
                pass
        if out:
            return out
        try:
            acm = sorted(glob.glob(_ACM_PATTERN))
        except Exception:
            acm = []
        # Only fall back to a bare ttyACM when it is unambiguous.
        return acm if len(acm) == 1 else []

    def _open(self, path: str) -> bool:
        fd = None
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[2] &= ~termios.HUPCL      # don't drop DTR on close (the
            attrs[4] = termios.B115200      # ESP32 must not see a reset pulse)
            attrs[5] = termios.B115200      # (CDC ignores baud; set anyway)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            try:
                termios.tcflush(fd, termios.TCIOFLUSH)
            except Exception:
                pass
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            return False
        self._fd = fd
        self._path = path
        self._rxbuf = b""
        self._txbuf = b""
        self._log("serial: using %s" % path)
        return True

    def _close(self, quiet: bool = False) -> None:
        fd = self._fd
        self._fd = None
        path = self._path
        self._path = None
        self._rxbuf = b""
        self._txbuf = b""
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            if not quiet and path:
                self._log("serial: lost %s" % path)

    @property
    def connected(self) -> bool:
        return self._fd is not None

    # -- pump (call every loop tick) ------------------------------------------ #
    def pump(self) -> None:
        """Open/rescan if closed (throttled), then drain inbound bytes."""
        if self._fd is None:
            now = time.monotonic()
            if now - self._last_scan < RESCAN_S:
                return
            self._last_scan = now
            for path in self._candidates():
                if self._open(path):
                    break
            if self._fd is None:
                return
        self._flush_tx()
        self._read_all()

    def _read_all(self) -> None:
        while self._fd is not None:
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                self._close()
                return
            except Exception:
                self._close()
                return
            if not chunk:
                # EOF on a tty: the device detached.
                self._close()
                return
            self._rxbuf += chunk
            if len(self._rxbuf) > _MAX_LINE * 4:
                # Runaway non-newline junk: keep only the tail, stay bounded.
                self._rxbuf = self._rxbuf[-_MAX_LINE:]
            self._consume_lines()

    def _consume_lines(self) -> None:
        while True:
            nl = self._rxbuf.find(b"\n")
            if nl < 0:
                return
            raw = self._rxbuf[:nl]
            self._rxbuf = self._rxbuf[nl + 1:]
            line = raw.strip()
            if not line or len(line) > _MAX_LINE:
                continue
            # Firmware debug prints ("[rubysat] boot", LVGL logs...) share
            # this channel; anything that isn't a JSON object is not a CMD.
            if not line.startswith(b"{"):
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(obj, dict):
                self._cmds.append(obj)

    # -- outbound -------------------------------------------------------------- #
    def broadcast(self, line: str) -> int:
        """Queue + write one STATE line. Returns 1 when the link is up (the
        line was written or buffered), 0 otherwise. NEVER raises."""
        if self._fd is None:
            return 0
        if not line.endswith("\n"):
            line = line + "\n"
        try:
            data = line.encode("utf-8")
        except Exception:
            return 0
        # Never split a line across a buffer reset: drop the whole backlog
        # if the device stopped draining, then start clean with this line.
        if len(self._txbuf) + len(data) > _MAX_TXBUF:
            self._txbuf = b""
        self._txbuf += data
        self._flush_tx()
        return 1 if self._fd is not None else 0

    def _flush_tx(self) -> None:
        while self._txbuf and self._fd is not None:
            try:
                n = os.write(self._fd, self._txbuf)
            except BlockingIOError:
                return
            except OSError:
                self._close()
                return
            except Exception:
                self._close()
                return
            if n <= 0:
                return
            self._txbuf = self._txbuf[n:]

    # -- inbound ----------------------------------------------------------------- #
    def commands(self) -> list:
        """Drain and return parsed inbound CMD dicts (possibly empty)."""
        out = list(self._cmds)
        self._cmds.clear()
        return out

    def send_line(self, line: str) -> bool:
        """Write one outbound line (wifi reply, etc.). Newline appended if
        missing. Returns True when queued/written on an open link."""
        if self._fd is None:
            return False
        if not line.endswith("\n"):
            line = line + "\n"
        try:
            data = line.encode("utf-8")
        except Exception:
            return False
        if len(self._txbuf) + len(data) > _MAX_TXBUF:
            self._txbuf = b""
        self._txbuf += data
        self._flush_tx()
        return self._fd is not None

    def stop(self) -> None:
        self._close(quiet=True)
