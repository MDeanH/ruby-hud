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
from .render import compose_frame, dock_target
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
    idx = page_idx % len(pages)
    if getattr(pages[idx], "hidden", False):   # never boot onto a hidden page
        idx = _first_visible(pages)
    return {"page_idx": idx, "pages": pages, "ctx": make_ctx(channel),
            "tap_fx": None, "_shown_idx": None, "nav_return": None}


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
        pages = ui["pages"]
        if cmd == "page_next":
            ui["page_idx"] = _visible_step(pages, ui["page_idx"], +1)
        elif cmd == "page_prev":
            ui["page_idx"] = _visible_step(pages, ui["page_idx"], -1)
    except Exception:
        pass


def _visible_step(pages, idx, direction) -> int:
    """Next VISIBLE page index in `direction` (+1/-1), skipping hidden pages."""
    n = len(pages)
    j = idx
    for _ in range(n):
        j = (j + direction) % n
        if not getattr(pages[j], "hidden", False):
            return j
    return idx


def _first_visible(pages) -> int:
    for i, p in enumerate(pages):
        if not getattr(p, "hidden", False):
            return i
    return 0


def _page_index_by_name(pages, name):
    for i, p in enumerate(pages):
        if getattr(p, "name", None) == name:
            return i
    return None


def _exit_hidden(ui, pages) -> int:
    """Leave a hidden page: return to where we deep-linked from (or first vis)."""
    ret = ui.get("nav_return")
    ui["nav_return"] = None
    if ret is not None and 0 <= ret < len(pages) \
            and not getattr(pages[ret], "hidden", False):
        return ret
    return _first_visible(pages)


def _consume_nav_request(ui, pages) -> None:
    """A menu action may have set ctx['nav_request'] to a page name; switch to
    that page (often hidden), remembering where to return."""
    req = ui["ctx"].get("nav_request")
    if not req:
        return
    ui["ctx"]["nav_request"] = None
    ti = _page_index_by_name(pages, req)
    if ti is not None and ti != ui["page_idx"]:
        ui["nav_return"] = ui["page_idx"]
        ui["page_idx"] = ti


def _dispatch_page_change(ui, pages) -> None:
    """Single source of truth for page-activation hooks: whenever page_idx has
    moved since last frame, fire on_hide(old) + on_show(new). Catches every
    switch path (swipe/edge/hold/deep-link/exit-hidden/remote). Lets a page set
    up / tear down on entry (e.g. PLAYBACK opens/releases its decoder) instead
    of inferring it from render timing."""
    shown = ui.get("_shown_idx")
    cur = ui["page_idx"]
    if shown == cur:
        return
    if shown is not None and 0 <= shown < len(pages):
        try:
            pages[shown].on_hide(ui["ctx"])
        except Exception:
            _log("on_hide error:\n" + traceback.format_exc())
    try:
        pages[cur].on_show(ui["ctx"])
    except Exception:
        _log("on_show error:\n" + traceback.format_exc())
    ui["_shown_idx"] = cur


def _apply_touch(ui, touch, pages, state) -> None:
    """Drain gestures and mutate ui (skips hidden pages in the swipe rotation;
    deep-links to hidden pages via ctx['nav_request']; tap_fx for feedback)."""
    old_idx = ui["page_idx"]
    for ev in touch.events():
        kind, ex, ey = ev[0], ev[1], ev[2]
        idx = ui["page_idx"]
        hidden = getattr(pages[idx], "hidden", False)
        if kind == "swipe_left":
            ui["page_idx"] = (_exit_hidden(ui, pages) if hidden
                              else _visible_step(pages, idx, +1))
        elif kind == "swipe_right":
            ui["page_idx"] = (_exit_hidden(ui, pages) if hidden
                              else _visible_step(pages, idx, -1))
        elif kind == "hold":
            consumed = False
            try:
                consumed = bool(pages[idx].handle_hold(ex, ey, ui["ctx"]))
            except Exception:
                _log("hold handler error:\n" + traceback.format_exc())
            if not consumed:
                ui["page_idx"] = (_exit_hidden(ui, pages) if hidden
                                  else _visible_step(pages, idx, +1))
        elif kind in ("swipe_up", "swipe_down"):
            try:
                pages[idx].handle_swipe_v(
                    "up" if kind == "swipe_up" else "down", ui["ctx"])
            except Exception:
                _log("swipe handler error:\n" + traceback.format_exc())
        elif kind == "tap":
            # Dock icon nav (visible pages only) takes priority; then edge-tap
            # prev/next; otherwise the tap goes to the page. On hidden pages all
            # taps go to the page (e.g. CAN pause / PLAYBACK pause).
            dock_to = None if hidden else dock_target(ex, ey, pages)
            if dock_to is not None:
                ui["page_idx"] = dock_to
            elif not hidden and ex < EDGE_PX:
                ui["page_idx"] = _visible_step(pages, idx, -1)
            elif not hidden and ex > EDGE_RIGHT_PX:
                ui["page_idx"] = _visible_step(pages, idx, +1)
            else:
                try:
                    pages[idx].handle_tap(ex, ey, ui["ctx"])
                except Exception:
                    _log("tap handler error:\n" + traceback.format_exc())
                ui["tap_fx"] = (ex, ey, time.time())
                _consume_nav_request(ui, pages)
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
            _dispatch_page_change(ui, pages)
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
