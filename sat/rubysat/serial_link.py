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

import glob
import json
import os
import time

try:
    import termios
    import tty
    _HAVE_TTY = True
except Exception:                      # non-POSIX / minimal build: degrade
    _HAVE_TTY = False

RETRY_S = 2.0
MAX_BUF = 8192
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
        self._buf = b""
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

    # -- outbound STATE --------------------------------------------------- #
    def broadcast(self, line: str) -> int:
        """Write one JSON record to the cabled satellite. Returns 1 if written,
        0 if no port / write failed. Never raises."""
        if self._fd is None:
            self._open()
            if self._fd is None:
                return 0
        if not line.endswith("\n"):
            line = line + "\n"
        try:
            os.write(self._fd, line.encode("utf-8"))
            return 1
        except Exception:
            self._close()             # unplugged / EIO -> drop, re-detect later
            return 0

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
