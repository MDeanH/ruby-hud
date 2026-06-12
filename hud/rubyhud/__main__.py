"""rubyhud entrypoint.

Modes (via env):
  RUBYHUD_ONESHOT=1  -> render ONE frame, save PNG to RUBYHUD_PNG
                        (default /tmp/hud.png), exit 0. Never touches /dev/fb0
                        or the touch layer. RUBYHUD_DEMO=1 uses
                        DataLayer.demo_snapshot(); else a live DataLayer
                        snapshot. RUBYHUD_PAGE selects the page (default 0).
  (normal)           -> open the framebuffer, loop at ~15 fps blitting frames.
                        Touch gestures (evdev, optional) page between the
                        pages (GAUGES / CAN BUS / SYSTEM / SETTINGS / ...):
                        swipe left/right, tap the screen edges, or long-press
                        anywhere (pages may consume holds / vertical swipes
                        first, e.g. SETTINGS uses hold=back, swipe=scroll).

Signals: SIGTERM/SIGINT -> clean shutdown (stop dl, clear+close fb, exit 0).
         SIGUSR1 -> save current frame to /tmp/hud.png.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import traceback

from .pages import make_ctx, make_pages
from .render import compose_frame
from .signals import DataLayer
from .touch import TouchInput

_LOG = "/tmp/rubyhud.log"
_DEFAULT_PNG = "/tmp/hud.png"

W, H = 1280, 800
EDGE_PX = 130          # tap zones: x < 130 -> prev page, x > 1150 -> next
EDGE_RIGHT_PX = 1150


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _make_ui(channel: str, page_idx: int = 0) -> dict:
    pages = make_pages()
    return {"page_idx": page_idx % len(pages), "pages": pages,
            "ctx": make_ctx(channel), "tap_fx": None}


# --- oneshot ---------------------------------------------------------------
def _oneshot() -> int:
    png = os.environ.get("RUBYHUD_PNG", _DEFAULT_PNG)
    channel = os.environ.get("RUBYHUD_CHANNEL", "can0")
    if os.environ.get("RUBYHUD_DEMO") == "1":
        snap = DataLayer.demo_snapshot()
    else:
        dl = DataLayer(channel)
        dl.start()
        time.sleep(0.2)
        snap = dl.snapshot()
        dl.stop()
    try:
        page_idx = int(os.environ.get("RUBYHUD_PAGE", "0"))
    except ValueError:
        page_idx = 0
    ui = _make_ui(channel, page_idx)
    img = compose_frame(snap, W, H, ui)
    img.save(png)
    print("oneshot wrote %s (%dx%d) page=%d"
          % (png, img.width, img.height, ui["page_idx"]))
    return 0


# --- normal loop -----------------------------------------------------------
class _Runner:
    def __init__(self):
        self.stop = False
        self.save_now = False
        self.last_img = None

    def on_term(self, *_):
        self.stop = True

    def on_usr1(self, *_):
        self.save_now = True


def _error_frame():
    """Minimal frame when compose_frame itself fails."""
    from PIL import Image, ImageDraw
    from .theme import BG, DANGER, font
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 360, W, 440], fill=DANGER)
    d.text((40, 380), "RUBYHUD RENDER ERROR", font=font(40, "bold"),
           fill=(255, 255, 255))
    return img


_REMOTE_CMD = "/dev/shm/rubyhud-remote.json"


def _apply_remote(ui, state) -> None:
    """Consume satellite commands (written by rubysat) — page flips for now.

    File: /dev/shm/rubyhud-remote.json {"seq": int, "cmd": "page_next"|...}.
    mtime-gated, seq-deduped, never raises."""
    try:
        st = os.stat(_REMOTE_CMD)
    except OSError:
        return
    if st.st_mtime_ns == state.get("remote_mtime"):
        return
    state["remote_mtime"] = st.st_mtime_ns
    try:
        import json
        with open(_REMOTE_CMD) as fh:
            doc = json.load(fh)
        seq = int(doc.get("seq", 0))
        if seq == state.get("remote_seq"):
            return
        state["remote_seq"] = seq
        cmd = str(doc.get("cmd", ""))
        n = len(ui["pages"])
        if cmd == "page_next":
            ui["page_idx"] = (ui["page_idx"] + 1) % n
        elif cmd == "page_prev":
            ui["page_idx"] = (ui["page_idx"] - 1) % n
    except Exception:
        pass


def _apply_touch(ui, touch, pages, state) -> None:
    """Drain gestures and mutate ui (page_idx wraps; tap_fx for feedback)."""
    n = len(pages)
    old_idx = ui["page_idx"]
    for ev in touch.events():
        kind, ex, ey = ev[0], ev[1], ev[2]
        if kind == "swipe_left":
            ui["page_idx"] = (ui["page_idx"] + 1) % n
        elif kind == "swipe_right":
            ui["page_idx"] = (ui["page_idx"] - 1) % n
        elif kind == "hold":
            consumed = False
            try:
                consumed = bool(pages[ui["page_idx"]].handle_hold(
                    ex, ey, ui["ctx"]))
            except Exception:
                _log("hold handler error:\n" + traceback.format_exc())
            if not consumed:
                # Fallback gesture: long-press anywhere cycles pages too.
                ui["page_idx"] = (ui["page_idx"] + 1) % n
        elif kind in ("swipe_up", "swipe_down"):
            try:
                pages[ui["page_idx"]].handle_swipe_v(
                    "up" if kind == "swipe_up" else "down", ui["ctx"])
            except Exception:
                _log("swipe handler error:\n" + traceback.format_exc())
        elif kind == "tap":
            if ex < EDGE_PX:
                ui["page_idx"] = (ui["page_idx"] - 1) % n
            elif ex > EDGE_RIGHT_PX:
                ui["page_idx"] = (ui["page_idx"] + 1) % n
            else:
                try:
                    pages[ui["page_idx"]].handle_tap(ex, ey, ui["ctx"])
                except Exception:
                    _log("tap handler error:\n" + traceback.format_exc())
                ui["tap_fx"] = (ex, ey, time.time())
    if ui["page_idx"] != old_idx:
        now = time.monotonic()
        if now - state.get("last_page_log", 0.0) >= 1.0:
            state["last_page_log"] = now
            _log("page -> %d (%s)"
                 % (ui["page_idx"], pages[ui["page_idx"]].name))


def _normal() -> int:
    from .framebuffer import FrameBuffer

    runner = _Runner()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, runner.on_term)
    try:
        signal.signal(signal.SIGUSR1, runner.on_usr1)
    except Exception:
        pass

    channel = os.environ.get("RUBYHUD_CHANNEL", "can0")
    dl = DataLayer(channel)
    dl.start()

    touch = TouchInput(W, H)
    touch.start()

    ui = _make_ui(channel)
    pages = ui["pages"]
    state = {"last_page_log": 0.0}

    try:
        fb = FrameBuffer()
    except Exception as exc:
        _log("framebuffer init failed: %s" % exc)
        traceback.print_exc()
        dl.stop()
        touch.stop()
        return 1

    period = 1.0 / 15.0
    last_err_log = 0.0
    try:
        while not runner.stop:
            t0 = time.monotonic()
            _apply_touch(ui, touch, pages, state)
            _apply_remote(ui, state)
            try:
                snap = dl.snapshot()
                img = compose_frame(snap, W, H, ui)
                runner.last_img = img
            except Exception:
                now = time.monotonic()
                if now - last_err_log >= 1.0:
                    last_err_log = now
                    _log("frame error:\n" + traceback.format_exc())
                img = _error_frame()
            try:
                fb.blit(img)
            except Exception:
                now = time.monotonic()
                if now - last_err_log >= 1.0:
                    last_err_log = now
                    _log("blit error:\n" + traceback.format_exc())

            if runner.save_now:
                runner.save_now = False
                try:
                    if runner.last_img is not None:
                        runner.last_img.save(_DEFAULT_PNG)
                except Exception:
                    _log("usr1 save failed:\n" + traceback.format_exc())

            dt = time.monotonic() - t0
            if dt < period:
                time.sleep(period - dt)
    finally:
        dl.stop()
        touch.stop()
        try:
            fb.clear()
            fb.close()
        except Exception:
            pass
    return 0


def main() -> int:
    if os.environ.get("RUBYHUD_ONESHOT") == "1":
        return _oneshot()
    return _normal()


if __name__ == "__main__":
    sys.exit(main())
