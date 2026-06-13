"""USB-serial STATE link to the Qualia satellite.

Mirrors the parts of TcpStateServer the publish loop uses -- broadcast() and
commands() -- so rubysat can serve the satellite over a direct USB cable in
addition to TCP/Wi-Fi. The Qualia (ESP32-S3) enumerates as a USB CDC ACM
device (/dev/ttyACM*) when cabled to the Pi; we write newline-delimited STATE
JSON to it and read newline-delimited CMD JSON back.

The Pi serves BOTH transports unconditionally; the satellite firmware decides
which one to consume (USB preferred, Wi-Fi fallback). So this link is additive:
when no Qualia is cabled it is simply a no-op that periodically re-scans.

No third-party dependency: the tty is opened raw via termios (CDC ACM ignores
the line speed, so no baud setup is needed). Hot-plug safe -- a vanished or
erroring port is closed and re-detected at most every RETRY_S. Never raises out
of broadcast()/commands(): a serial hiccup must not kill the publish loop.
"""

from __future__ import annotations

import errno
import glob
import json
import os
import select
import time

try:
    import termios
    import tty
    _HAVE_TTY = True
except Exception:                      # non-POSIX / minimal build: degrade
    _HAVE_TTY = False

RETRY_S = 2.0
MAX_BUF = 8192
WRITE_BUDGET_S = 0.15   # max time broadcast() may spend draining TX backpressure
_BYID = "/dev/serial/by-id"


def _find_port() -> str | None:
    """Pick the Qualia's serial device. Prefer a stable by-id symlink that looks
    like an Espressif/ESP32-S3 CDC; fall back to the first /dev/ttyACM*."""
    try:
        for name in sorted(os.listdir(_BYID)):
            low = name.lower()
            if any(k in low for k in ("espressif", "esp32", "qualia", "cdc",
                                      "usb_jtag", "serial_jtag")):
                return os.path.join(_BYID, name)
    except Exception:
        pass
    acms = sorted(glob.glob("/dev/ttyACM*"))
    return acms[0] if acms else None


class SerialStateLink:
    """Drop-in companion to TcpStateServer for the USB cable path."""

    def __init__(self, log=None):
        self._fd: int | None = None
        self._buf = b""           # inbound (CMD) partial-line accumulator
        self._wbuf = b""          # outbound (STATE) remainder after a short write
        self._last_try = 0.0
        self._path: str | None = None
        self._log = log or (lambda *_: None)

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        self._open()

    def stop(self) -> None:
        self._close()

    def is_up(self) -> bool:
        return self._fd is not None

    def _open(self) -> None:
        if self._fd is not None or not _HAVE_TTY:
            return
        now = time.monotonic()
        if now - self._last_try < RETRY_S:
            return
        self._last_try = now
        path = _find_port()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except Exception:
            return
        try:
            tty.setraw(fd)            # cfmakeraw equivalent (no echo/canon)
        except Exception:
            pass
        self._fd = fd
        self._path = path
        self._buf = b""
        self._log("serial link up on %s" % path)

    def _close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._log("serial link down (%s)" % self._path)
        self._fd = None
        self._buf = b""
        self._wbuf = b""

    # -- outbound STATE --------------------------------------------------- #
    def broadcast(self, line: str) -> int:
        """Write one JSON record to the cabled satellite. Returns 1 if the
        buffer fully drained, 0 if no port / a remainder is left buffered /
        the link dropped. Never raises and never blocks the publish loop for
        more than ~WRITE_BUDGET_S.

        Robust like sock.sendall(): honors short writes (the unwritten tail is
        kept in _wbuf and prepended next call, so a truncated JSON line never
        reaches the firmware) and treats EAGAIN/EWOULDBLOCK as transient TX
        backpressure (keep the fd; do NOT tear down a healthy cable). Only a
        real device error (EIO/ENODEV/ENXIO/EBADF) closes + re-detects."""
        if self._fd is None:
            self._open()
            if self._fd is None:
                return 0
        if not line.endswith("\n"):
            line = line + "\n"
        self._wbuf += line.encode("utf-8")
        if len(self._wbuf) > MAX_BUF:
            # Cable persistently slower than the publish rate: drop the backlog
            # and resync on this line (one corrupt line, then clean framing).
            self._wbuf = line.encode("utf-8")
        deadline = time.monotonic() + WRITE_BUDGET_S
        while self._wbuf:
            try:
                n = os.write(self._fd, self._wbuf)
                self._wbuf = self._wbuf[n:]
            except (BlockingIOError, InterruptedError):
                if time.monotonic() >= deadline:
                    return 0          # keep remainder buffered; never block long
                select.select([], [self._fd], [], 0.02)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    if time.monotonic() >= deadline:
                        return 0
                    select.select([], [self._fd], [], 0.02)
                    continue
                self._close()         # EIO/ENODEV/ENXIO/EBADF: real unplug
                return 0
            except Exception:
                self._close()
                return 0
        return 1

    # -- inbound commands ------------------------------------------------- #
    def commands(self) -> list:
        """Drain and return inbound CMD dicts from the cable (possibly empty)."""
        if self._fd is None:
            return []
        try:
            while True:
                try:
                    chunk = os.read(self._fd, 4096)
                except BlockingIOError:
                    break
                except Exception:
                    self._close()
                    return []
                if not chunk:
                    break
                self._buf += chunk
                if len(self._buf) > MAX_BUF:
                    self._buf = self._buf[-1024:]    # runaway-line guard
        except Exception:
            self._close()
            return []
        out: list = []
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            raw = raw.strip()
            if not raw.startswith(b"{"):             # skip debug/boot noise
                continue
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(d, dict):
                    out.append(d)
            except Exception:
                pass
        return out
