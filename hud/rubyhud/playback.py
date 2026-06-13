"""PLAYBACK page: review recorded screen/camera clips on the HUD.

Hidden page, reached via CONFIGURE > RECORDING > PLAYBACK. Lists the MP4s in
~/recordings newest-first; tap one to play. Decoding is cv2.VideoCapture (the
Pi 5 has hardware H.264 *decode*); one frame is pulled per render call, paced to
the clip's fps, and blitted (letterboxed) into the page body. tap = pause/resume,
hold = stop/back. Page entry is detected by a gap in render() calls (the page
only renders while active), which resets to the list and releases the decoder.

Everything is failure-guarded: missing cv2, no files, or a bad clip degrades to
a message and never raises into the render loop.
"""

from __future__ import annotations

import os
import time

from . import gauges, recorder, theme
from .render import SH, SS, SW
from .theme import (ACCENT, CARD_BORDER, DANGER, OK, PANEL, TEXT, TEXT_DIM,
                    WARN, font, mix)

try:
    import cv2
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False

BG = theme.BG


class PlaybackPage:
    name = "PLAYBACK"
    hidden = True

    # Body area (screen px) between the global top/bottom strips.
    AREA_X0, AREA_Y0, AREA_X1, AREA_Y1 = 24, 70, 1256, 740
    ROW_H = 62
    LIST_TOP = 116
    MAX_ROWS = 9

    def __init__(self):
        self._mode = "list"          # 'list' | 'play'
        self._files: list = []
        self._cap = None
        self._cur_path = None
        self._paused = False
        self._frame = None           # cached (ox, oy, PIL Image) for pause/hold
        self._fps = 15.0
        self._next_due = 0.0

    # -- lifecycle (driven by the nav layer via on_show/on_hide) ---------- #
    def on_show(self, ctx):
        """Page became active: refresh the clip list, start at the list view."""
        self._stop_cap()
        self._mode = "list"
        self._files = recorder.list_recordings()
        self._paused = False
        self._frame = None

    def on_hide(self, ctx):
        """Page left (incl. swipe-away mid-play): release the decoder so we
        never hold an open VideoCapture / file handle while inactive."""
        self._stop_cap()
        self._mode = "list"
        self._paused = False
        self._frame = None

    def _stop_cap(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        self._cur_path = None

    def _open(self, path):
        self._stop_cap()
        if not _HAVE_CV2:
            return
        try:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                cap.release()
                return
            fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
            self._fps = fps if 5.0 <= fps <= 60.0 else 15.0
            self._cap = cap
            self._cur_path = path
            self._mode = "play"
            self._paused = False
            self._frame = None
            self._next_due = time.monotonic()
        except Exception:
            self._stop_cap()

    def _rows(self):
        return self._files[:self.MAX_ROWS]

    # -- render ----------------------------------------------------------- #
    def render_static(self, draw, img):
        pass  # fully dynamic

    def render(self, draw, img, snap, ctx):
        # Entry/exit is handled by on_show/on_hide (nav layer), so a slow decode
        # frame can never be misread as "navigated away".
        if self._mode == "play":
            self._render_play(draw, img, time.monotonic())
        else:
            self._render_list(draw, img)

    def _render_list(self, draw, img):
        gauges.tracked_text(draw, 40 * SS, 80 * SS, "PLAYBACK  ·  RECORDINGS",
                            font(24 * SS, "bold"), TEXT, tracking=3 * SS)
        if not _HAVE_CV2:
            gauges._centered_text(draw, SW // 2, SH // 2,
                                  "VIDEO DECODE UNAVAILABLE",
                                  font(30 * SS, "bold"), DANGER)
            return
        if not self._files:
            gauges._centered_text(draw, SW // 2, SH // 2, "NO RECORDINGS",
                                  font(32 * SS, "bold"), TEXT_DIM)
            gauges._centered_text(draw, SW // 2, SH // 2 + 46 * SS,
                                  "record from CONFIGURE > RECORDING",
                                  font(20 * SS, "regular"), TEXT_DIM)
            return
        x0, x1 = self.AREA_X0 * SS, self.AREA_X1 * SS
        y = self.LIST_TOP
        for path in self._rows():
            ry = y * SS
            draw.rounded_rectangle([x0, ry, x1, ry + (self.ROW_H - 8) * SS],
                                   radius=10 * SS, fill=PANEL,
                                   outline=CARD_BORDER, width=SS)
            base = os.path.basename(path)
            kind = ("SCREEN" if base.startswith("screen")
                    else "CAMERA" if base.startswith("camera") else "CLIP")
            col = ACCENT if kind == "SCREEN" else OK
            draw.text((x0 + 18 * SS, ry + 12 * SS), base,
                      font=font(20 * SS, "mono"), fill=TEXT)
            try:
                meta = "%s  %.1f MB" % (kind, os.path.getsize(path) / 1048576.0)
            except Exception:
                meta = kind
            mfont = font(18 * SS, "bold")
            try:
                tw = draw.textlength(meta, font=mfont)
            except Exception:
                tw = 0
            draw.text((x1 - 18 * SS - tw, ry + 13 * SS), meta, font=mfont,
                      fill=col)
            y += self.ROW_H
        gauges._centered_text(draw, SW // 2, (self.AREA_Y1 + 6) * SS,
                              "tap a clip to play   .   hold to go back",
                              font(17 * SS, "regular"), mix(BG, TEXT_DIM, 0.75))

    def _render_play(self, draw, img, now):
        if self._cap is None:
            self._mode = "list"
            return
        if not self._paused and now >= self._next_due:
            ok, frame = self._cap.read()
            if not ok:                       # end of clip -> back to the list
                self._stop_cap()
                self._mode = "list"
                self._files = recorder.list_recordings()
                return
            self._frame = self._fit(frame)
            self._next_due = now + 1.0 / self._fps
        if self._frame is not None:
            ox, oy, fimg = self._frame
            try:
                img.paste(fimg, (ox, oy))
            except Exception:
                pass
        if self._cur_path:
            gauges.status_chip(draw, 30 * SS, 74 * SS,
                               os.path.basename(self._cur_path), ACCENT,
                               filled=True, scale=SS)
        if self._paused:
            gauges._centered_text(draw, SW // 2, SH // 2, "PAUSED",
                                  font(44 * SS, "bold"), WARN)
        gauges._centered_text(draw, SW // 2, (self.AREA_Y1 + 6) * SS,
                              "tap pause/play   .   hold to stop",
                              font(17 * SS, "regular"), mix(BG, TEXT_DIM, 0.75))

    def _fit(self, frame):
        """BGR ndarray -> (ox, oy, PIL RGB Image) letterboxed in the body."""
        from PIL import Image
        h, w = frame.shape[:2]
        aw = (self.AREA_X1 - self.AREA_X0) * SS
        ah = (self.AREA_Y1 - self.AREA_Y0) * SS
        scale = min(aw / float(w), ah / float(h))
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        small = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        ox = self.AREA_X0 * SS + (aw - dw) // 2
        oy = self.AREA_Y0 * SS + (ah - dh) // 2
        return (ox, oy, Image.fromarray(rgb))

    # -- input ------------------------------------------------------------ #
    def handle_tap(self, x, y, ctx):
        if self._mode == "play":
            self._paused = not self._paused
            return True
        rows = self._rows()
        if not rows:
            return False
        if self.LIST_TOP <= y < self.LIST_TOP + len(rows) * self.ROW_H:
            idx = (y - self.LIST_TOP) // self.ROW_H
            if 0 <= idx < len(rows):
                self._open(rows[idx])
        return True

    def handle_hold(self, x, y, ctx):
        if self._mode == "play":
            self._stop_cap()
            self._mode = "list"
            self._files = recorder.list_recordings()
            return True       # consumed: stay on page, back to the list
        return False          # list: let the loop pop back to CONFIGURE

    def handle_swipe_v(self, direction, ctx):
        return False
