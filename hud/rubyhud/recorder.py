"""HUD screen + camera recording for rubyhud.

Two independent recorders, each a managed ffmpeg subprocess writing an MP4 into
~/recordings/:

  * SCREEN  -- ffmpeg fbdev capture of /dev/fb0 (exactly what's on the panel).
  * CAMERA  -- the rubyvision *annotated* feed: a feeder thread tails
               /dev/shm/rubyvision/frame.jpg and pipes whole JPEGs into ffmpeg
               (image2pipe). This never contends with rubyvision for the CSI
               camera, and records the boxes/labels the AI is drawing.

The Pi 5 has no hardware H.264 encoder, so we use libx264 -preset ultrafast to
keep CPU off the render loop. Everything is failure-guarded: missing ffmpeg, an
unreadable framebuffer, or a dead vision feed degrades to 'not recording' and
never raises. Stopping sends ffmpeg SIGINT (camera also closes the pipe) so the
MP4 trailer is written and the file is playable.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import threading
import time

REC_DIR = os.environ.get(
    "RUBYHUD_REC_DIR", os.path.join(os.path.expanduser("~"), "recordings"))
FB_DEV = os.environ.get("RUBYHUD_FB", "/dev/fb0")
VISION_FRAME = "/dev/shm/rubyvision/frame.jpg"
_FPS = 15
_X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
         "-pix_fmt", "yuv420p"]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


class _Rec:
    """One recorder (kind = 'screen' | 'camera')."""

    def __init__(self, kind: str):
        self.kind = kind
        self._proc = None
        self._feeder = None
        self._stop = threading.Event()
        self._file = None
        self._last_file = None
        self._t0 = 0.0
        self._lock = threading.Lock()

    # -- state ------------------------------------------------------------ #
    def is_active(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def duration(self) -> float:
        return (time.monotonic() - self._t0) if self.is_active() else 0.0

    def last_file(self):
        return self._last_file

    # -- control ---------------------------------------------------------- #
    def start(self) -> bool:
        with self._lock:
            if self.is_active():
                return True
            if not have_ffmpeg():
                return False
            try:
                os.makedirs(REC_DIR, exist_ok=True)
            except Exception:
                return False
            out = os.path.join(REC_DIR, "%s-%s.mp4" % (self.kind, _ts()))
            try:
                ok = (self._start_screen(out) if self.kind == "screen"
                      else self._start_camera(out))
            except Exception:
                ok = False
            if ok:
                self._file = out
                self._last_file = out
                self._t0 = time.monotonic()
            else:
                self._proc = None
            return ok

    def _start_screen(self, out: str) -> bool:
        cmd = (["ffmpeg", "-nostdin", "-loglevel", "error",
                "-f", "fbdev", "-framerate", str(_FPS), "-i", FB_DEV]
               + _X264 + ["-y", out])
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return True

    def _start_camera(self, out: str) -> bool:
        cmd = (["ffmpeg", "-loglevel", "error",
                "-f", "image2pipe", "-framerate", str(_FPS), "-i", "-"]
               + _X264 + ["-y", out])
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self._stop.clear()
        self._feeder = threading.Thread(
            target=self._feed, name="rubyhud-rec-cam", daemon=True)
        self._feeder.start()
        return True

    def _feed(self) -> None:
        """Tail the vision JPEG and pipe whole frames to ffmpeg at ~_FPS."""
        period = 1.0 / _FPS
        proc = self._proc
        nxt = time.monotonic()
        while not self._stop.is_set() and proc is not None and proc.poll() is None:
            try:
                with open(VISION_FRAME, "rb") as fh:
                    data = fh.read()
                # Only forward complete JPEGs (SOI..EOI) -- skip a mid-write read.
                if (len(data) > 4 and data[:2] == b"\xff\xd8"
                        and data[-2:] == b"\xff\xd9" and proc.stdin):
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                break
            except Exception:
                pass
            nxt += period
            dt = nxt - time.monotonic()
            if dt > 0:
                self._stop.wait(dt)
            else:
                nxt = time.monotonic()
        try:
            if proc is not None and proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

    @staticmethod
    def _reap(proc) -> None:
        """SIGINT -> wait -> escalate. ffmpeg finalizes the MP4 trailer on
        SIGINT; terminate/kill are the fallback. May block up to ~8s, so this
        runs OFF the render thread (see stop())."""
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stop(self, blocking: bool = False) -> None:
        """Stop the recording. Marks inactive immediately so the caller (the
        ~30fps render thread, via a menu tap) never blocks; the ffmpeg teardown
        is reaped on a daemon thread. At process shutdown pass blocking=True so
        the trailer is written before the interpreter exits."""
        with self._lock:
            self._stop.set()
            p = self._proc
            self._proc = None       # is_active() -> False at once
            self._file = None
        if p is None or p.poll() is not None:
            return
        if blocking:
            self._reap(p)
        else:
            threading.Thread(target=self._reap, args=(p,),
                             name="rubyhud-rec-stop", daemon=True).start()


_screen = _Rec("screen")
_camera = _Rec("camera")


# --------------------------------------------------------------------------- #
# Module API (used by the CONFIGURE > Recording menu)
# --------------------------------------------------------------------------- #
def toggle_screen() -> None:
    _screen.stop() if _screen.is_active() else _screen.start()


def toggle_camera() -> None:
    _camera.stop() if _camera.is_active() else _camera.start()


def stop_all() -> None:
    """Stop both recorders, BLOCKING so MP4 trailers are written before exit.
    Registered with atexit so a rubyhud restart never orphans an ffmpeg child
    (the screen recorder reads /dev/fb0 with no auto-stop and would otherwise
    keep capturing + leave a trailer-less, unplayable file)."""
    _screen.stop(blocking=True)
    _camera.stop(blocking=True)


atexit.register(stop_all)


def _status(rec: _Rec) -> str:
    if not have_ffmpeg():
        return "no ffmpeg"
    if rec.is_active():
        d = int(rec.duration())
        return "REC %d:%02d" % (d // 60, d % 60)
    return "off"


def screen_status() -> str:
    return _status(_screen)


def camera_status() -> str:
    return _status(_camera)


def any_active() -> bool:
    return _screen.is_active() or _camera.is_active()


def last_file_name() -> str:
    """Basename of the most recent recording from either recorder, or '--'."""
    cand = [r.last_file() for r in (_screen, _camera) if r.last_file()]
    if not cand:
        return "--"
    return os.path.basename(sorted(cand)[-1])
