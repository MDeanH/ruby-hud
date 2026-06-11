"""Frame sources for rubyvision.

A Source yields RGB frames (numpy HxWx3 uint8) one at a time. The protocol is
intentionally tiny:

    read() -> numpy HxWx3 RGB uint8 | None   (None == no frame this tick)
    name  : str                              (short identifier for status)
    close()                                  (release the underlying device)

Implementations:
  PatternSource  -- pure numpy, ALWAYS available; the guaranteed fallback.
  CsiSource      -- picamera2 (lazy import); Raspberry Pi camera.
  UvcSource      -- cv2.VideoCapture (lazy import); USB / UVC webcam.
  VideoSource    -- cv2.VideoCapture (lazy import); looped video file (demo).

Hardware deps are imported INSIDE each implementation so this module imports
fine on a bare pillow + numpy venv; a missing/broken device raises
SourceUnavailable and open_source() falls through to the next candidate.
"""

from __future__ import annotations

import glob
import math
import os
import time

import numpy as np

# Native capture resolution; everything downstream letterboxes from here.
CAP_W, CAP_H = 1280, 720


class SourceUnavailable(Exception):
    """Raised when a source cannot be opened (import failed / no device)."""


# --------------------------------------------------------------------------- #
# PatternSource (pure numpy, always available)
# --------------------------------------------------------------------------- #
class PatternSource:
    """Deterministic animated test pattern: dark grid background with two or
    three sprites (car rectangles, a person capsule) gliding on sinusoid paths.

    Motion is driven by a FRAME COUNTER (no RNG, no time-seeded randomness) so
    the scene is fully reproducible. `truth` holds the current ground-truth
    boxes (src-space pixels) for StubDetector to map into the 640 inference
    space. Paced to ~15 fps via a wall-clock gate so a tight consumer loop does
    not spin.
    """

    def __init__(self, fps: float = 15.0):
        self.name = "pattern"
        self._fps = max(1.0, float(fps))
        self._period = 1.0 / self._fps
        self._counter = 0
        self._next_t = 0.0
        self.truth: list = []
        self._bg = self._make_bg()

    @staticmethod
    def _make_bg() -> np.ndarray:
        """Dark charcoal background with a faint grid (built once)."""
        bg = np.empty((CAP_H, CAP_W, 3), dtype=np.uint8)
        bg[:, :, 0] = 12
        bg[:, :, 1] = 16
        bg[:, :, 2] = 22
        grid = (40, 48, 60)
        step = 80
        for x in range(0, CAP_W, step):
            bg[:, x, :] = grid
        for y in range(0, CAP_H, step):
            bg[y, :, :] = grid
        return bg

    def _sprites(self, t: int):
        """Return [(cls, (x0, y0, x1, y1), color)] for frame index t.

        Paths are pure sinusoids of the integer counter -> deterministic.
        """
        out = []
        # Car 1: wide rectangle sweeping left<->right across the lower half.
        cw, ch = 240, 130
        cx = int((CAP_W - cw) * (0.5 + 0.42 * math.sin(t * 0.018)))
        cy = int(CAP_H * 0.52 + 60 * math.sin(t * 0.012))
        out.append(("car", (cx, cy, cx + cw, cy + ch), (90, 150, 220)))

        # Car 2: smaller, opposite phase, upper band.
        cw2, ch2 = 180, 100
        cx2 = int((CAP_W - cw2) * (0.5 - 0.38 * math.sin(t * 0.022 + 1.0)))
        cy2 = int(CAP_H * 0.24 + 40 * math.sin(t * 0.02 + 2.0))
        out.append(("car", (cx2, cy2, cx2 + cw2, cy2 + ch2), (220, 150, 90)))

        # Person: tall capsule, slow lateral drift; appears every other span.
        pw, phh = 70, 200
        px = int((CAP_W - pw) * (0.5 + 0.30 * math.sin(t * 0.009 + 0.5)))
        py = int(CAP_H * 0.40 + 30 * math.sin(t * 0.015))
        out.append(("person", (px, py, px + pw, py + phh), (120, 220, 140)))
        return out

    def _render(self, t: int) -> np.ndarray:
        img = self._bg.copy()
        truth = []
        for cls, (x0, y0, x1, y1), col in self._sprites(t):
            x0 = max(0, min(CAP_W - 1, x0))
            x1 = max(0, min(CAP_W, x1))
            y0 = max(0, min(CAP_H - 1, y0))
            y1 = max(0, min(CAP_H, y1))
            if x1 <= x0 or y1 <= y0:
                continue
            if cls == "person":
                # Rounded-ish capsule: just a filled rect is fine for a stub.
                img[y0:y1, x0:x1] = col
                # darker head band
                hb = y0 + max(2, (y1 - y0) // 6)
                img[y0:hb, x0:x1] = tuple(int(c * 0.7) for c in col)
            else:
                img[y0:y1, x0:x1] = col
                # window strip for a car-ish read
                wy0 = y0 + (y1 - y0) // 5
                wy1 = y0 + (y1 - y0) // 2
                img[wy0:wy1, x0 + 16:x1 - 16] = (30, 40, 55)
            truth.append({"cls": cls, "box": [x0, y0, x1, y1]})
        self.truth = truth
        self._burn_clock(img)
        return img

    @staticmethod
    def _burn_clock(img: np.ndarray) -> None:
        """Burn a coarse wall-clock + 'PATTERN' tag using block digits so we
        avoid a font dependency in the capture thread (numpy only)."""
        txt = time.strftime("%H:%M:%S")
        _blit_blocks(img, 24, 24, "PATTERN " + txt, (200, 210, 225))

    def read(self):
        now = time.monotonic()
        if self._next_t == 0.0:
            self._next_t = now
        # Pace to ~fps; if we are early, signal "no frame yet".
        if now < self._next_t:
            return None
        self._next_t += self._period
        if self._next_t < now:  # fell behind; resync
            self._next_t = now + self._period
        frame = self._render(self._counter)
        self._counter += 1
        return frame

    def close(self):
        pass


# Tiny 5x7 block-font renderer (numpy) so PatternSource needs no PIL/font.
_GLYPHS = {
    " ": ["00000"] * 7,
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
}


def _blit_blocks(img: np.ndarray, x: int, y: int, text: str, color,
                 px: int = 3) -> None:
    """Draw `text` with the 5x7 block font into `img` at (x, y). px = pixel
    scale. Unknown chars render as blank. Never raises (clips to bounds)."""
    h, w = img.shape[:2]
    cx = x
    for ch in str(text).upper():
        glyph = _GLYPHS.get(ch, _GLYPHS[" "])
        for ry, row in enumerate(glyph):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                x0 = cx + rx * px
                y0 = y + ry * px
                x1 = min(w, x0 + px)
                y1 = min(h, y0 + px)
                if x0 >= w or y0 >= h or x1 <= 0 or y1 <= 0:
                    continue
                img[max(0, y0):y1, max(0, x0):x1] = color
        cx += 6 * px


# --------------------------------------------------------------------------- #
# CsiSource (picamera2, lazy import)
# --------------------------------------------------------------------------- #
class CsiSource:
    """Raspberry Pi CSI camera via picamera2, configured for 1280x720 RGB."""

    def __init__(self):
        self.name = "csi"
        try:
            from picamera2 import Picamera2  # lazy
        except Exception as exc:  # ImportError or arch/runtime failures
            raise SourceUnavailable("picamera2 import failed: %s" % exc)
        try:
            self._cam = Picamera2()
            cfg = self._cam.create_preview_configuration(
                main={"size": (CAP_W, CAP_H), "format": "RGB888"})
            self._cam.configure(cfg)
            self._cam.start()
        except Exception as exc:
            try:
                self._cam.close()
            except Exception:
                pass
            raise SourceUnavailable("picamera2 init failed: %s" % exc)

    def read(self):
        try:
            arr = self._cam.capture_array()
        except Exception:
            return None
        if arr is None:
            return None
        # picamera2 RGB888 main stream is already RGB; ensure 3-channel uint8.
        if arr.ndim == 3 and arr.shape[2] >= 3:
            return np.ascontiguousarray(arr[:, :, :3], dtype=np.uint8)
        return None

    def close(self):
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            self._cam.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# UvcSource (cv2, lazy import)
# --------------------------------------------------------------------------- #
class UvcSource:
    """USB / UVC webcam via cv2.VideoCapture (V4L2, MJPG, latest-frame)."""

    def __init__(self, index: int = 0):
        self.name = "usb"
        self._idx = int(index)
        try:
            import cv2  # lazy
        except Exception as exc:
            raise SourceUnavailable("cv2 import failed: %s" % exc)
        self._cv2 = cv2
        try:
            cap = cv2.VideoCapture(self._idx, cv2.CAP_V4L2)
            if not cap or not cap.isOpened():
                raise SourceUnavailable("VideoCapture(%d) not opened"
                                        % self._idx)
            cap.set(cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_H)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            self._cap = cap
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable("cv2 capture init failed: %s" % exc)
        self.name = "usb%d" % self._idx

    def read(self):
        try:
            ok, frame = self._cap.read()
        except Exception:
            return None
        if not ok or frame is None:
            return None
        # cv2 delivers BGR; convert to RGB.
        try:
            rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        except Exception:
            rgb = frame[:, :, ::-1]
        return np.ascontiguousarray(rgb, dtype=np.uint8)

    def close(self):
        try:
            self._cap.release()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# VideoSource (cv2, lazy import) -- looped demo file
# --------------------------------------------------------------------------- #
class VideoSource:
    """Looped video file via cv2.VideoCapture; rewinds at EOF (demo mode)."""

    def __init__(self, path: str):
        self.name = "video"
        self._path = str(path)
        try:
            import cv2  # lazy
        except Exception as exc:
            raise SourceUnavailable("cv2 import failed: %s" % exc)
        self._cv2 = cv2
        cap = cv2.VideoCapture(self._path)
        if not cap or not cap.isOpened():
            raise SourceUnavailable("cannot open video %s" % self._path)
        self._cap = cap

    def read(self):
        try:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                # Loop: rewind to the first frame.
                self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
            if not ok or frame is None:
                return None
            rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
            return np.ascontiguousarray(rgb, dtype=np.uint8)
        except Exception:
            return None

    def close(self):
        try:
            self._cap.release()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
# Cycle order used by the HUD's cmd "cycle_source": csi -> usb -> video ->
# pattern -> (wrap to csi). open_source(pref=...) accepts any of these names or
# "auto" (the same order, each guarded; pattern is the guaranteed fallback).
SOURCE_CYCLE = ("csi", "usb", "video", "pattern")


def _find_usb_index():
    """Lowest /dev/video[0-9]* index, or None."""
    nodes = sorted(glob.glob("/dev/video[0-9]*"))
    for node in nodes:
        try:
            return int(node[len("/dev/video"):])
        except ValueError:
            continue
    return None


def _find_demo(model_dir: str):
    """First demo video under /home/michael/vision/demo, or None.

    model_dir is the models directory; the demo dir is its sibling 'demo'.
    """
    candidates = []
    demo_dir = "/home/michael/vision/demo"
    candidates.append(demo_dir)
    if model_dir:
        candidates.append(os.path.join(os.path.dirname(
            os.path.normpath(model_dir)), "demo"))
    for d in candidates:
        for ext in ("mp4", "mov", "m4v", "avi", "mkv"):
            hits = sorted(glob.glob(os.path.join(d, "*." + ext)))
            if hits:
                return hits[0]
    return None


def _open_one(kind: str, model_dir: str):
    """Open a single source by kind. Raises SourceUnavailable on failure."""
    if kind == "csi":
        return CsiSource()
    if kind == "usb":
        idx = _find_usb_index()
        if idx is None:
            raise SourceUnavailable("no /dev/video* node")
        return UvcSource(idx)
    if kind == "video":
        demo = _find_demo(model_dir)
        if not demo:
            raise SourceUnavailable("no demo video file")
        return VideoSource(demo)
    if kind == "pattern":
        return PatternSource()
    raise SourceUnavailable("unknown source kind %r" % kind)


def open_source(pref: str = "auto", model_dir: str = ""):
    """Open a source. Returns (source, kind).

    pref="auto" tries csi -> usb -> video -> pattern, each guarded; a specific
    kind tries only that kind but still falls back to pattern on failure so the
    pipeline always has a frame producer.
    """
    pref = (pref or "auto").lower()
    if pref == "auto":
        order = SOURCE_CYCLE
    else:
        # Try the requested kind first, then the rest of the cycle.
        rest = tuple(k for k in SOURCE_CYCLE if k != pref)
        order = (pref,) + rest
    last_err = None
    for kind in order:
        try:
            src = _open_one(kind, model_dir)
            return src, src.name if hasattr(src, "name") else kind
        except SourceUnavailable as exc:
            last_err = exc
            continue
        except Exception as exc:  # be defensive: never let one source kill us
            last_err = exc
            continue
    # Absolute fallback (should be unreachable: pattern never fails).
    src = PatternSource()
    return src, "pattern"
