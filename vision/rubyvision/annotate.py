"""Letterboxing, box coordinate mapping, and PIL overlay drawing.

Geometry chain:
    source (CAP_WxCAP_H) --letterbox--> 640x640 (detector input)
    detection box (640 space) --map_box_640_to_src--> source space
    source box --map_box_src_to_preview--> 800x450 preview space

draw_overlay() paints the detections (and an optional DEMO badge) onto the
800x450 preview with PIL and returns the annotated RGB image.

PIL is a hard expectation of the whole stack (shared with rubyhud); numpy too.
No hardware deps here.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .sources import CAP_H, CAP_W

INFER_SIZE = 640
PREVIEW_W, PREVIEW_H = 800, 450

# Palette (mirrors rubyhud.theme so the preview matches the HUD look).
ACCENT = (208, 39, 59)        # Soul Red
ACCENT_GLOW = (255, 77, 92)
AMBER = (255, 179, 0)         # WARN
TEXT = (243, 247, 251)
DARK = (10, 12, 15)


# --------------------------------------------------------------------------- #
# fonts (best-effort; never raise)
# --------------------------------------------------------------------------- #
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_font_cache: dict = {}


def _font(size: int):
    size = max(8, int(size))
    f = _font_cache.get(size)
    if f is not None:
        return f
    font = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
    _font_cache[size] = font
    return font


# --------------------------------------------------------------------------- #
# letterbox
# --------------------------------------------------------------------------- #
def letterbox(rgb, size: int = INFER_SIZE):
    """Resize an RGB numpy frame into a centered `size`x`size` square with
    gray padding, preserving aspect. Returns (img640_uint8, scale, padx, pady).

    scale/padx/pady describe src->square: square_xy = src_xy * scale + pad.
    """
    arr = np.ascontiguousarray(rgb)
    h, w = arr.shape[:2]
    if w <= 0 or h <= 0:
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        return canvas, 1.0, 0.0, 0.0
    scale = min(size / float(w), size / float(h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    # Resize via PIL (no cv2 dependency).
    src_img = Image.fromarray(arr[:, :, :3].astype(np.uint8), "RGB")
    resized = src_img.resize((new_w, new_h), Image.BILINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)  # YOLO gray pad
    padx = (size - new_w) // 2
    pady = (size - new_h) // 2
    canvas[pady:pady + new_h, padx:padx + new_w] = np.asarray(resized)
    return canvas, float(scale), float(padx), float(pady)


def map_box_640_to_src(box, scale: float, padx: float, pady: float,
                       src_w: int = CAP_W, src_h: int = CAP_H):
    """Map a [x0, y0, x1, y1] box from 640-square space back to source-frame
    pixels, undoing the letterbox transform, and clamp to the ACTUAL source
    dims (src_w x src_h).

    The clamp bounds MUST be the real frame size (the same dims used to build
    scale/padx/pady and to feed map_box_src_to_preview), not the CAP_W/CAP_H
    request -- a UVC cam that ignored the resolution request, a demo .mp4 of a
    different size, or a non-720p CSI mode would otherwise be clamped against
    the wrong bounds and corrupt the boxes."""
    x0, y0, x1, y1 = box
    sx0 = (x0 - padx) / scale
    sy0 = (y0 - pady) / scale
    sx1 = (x1 - padx) / scale
    sy1 = (y1 - pady) / scale
    sx0 = max(0.0, min(float(src_w), sx0))
    sx1 = max(0.0, min(float(src_w), sx1))
    sy0 = max(0.0, min(float(src_h), sy0))
    sy1 = max(0.0, min(float(src_h), sy1))
    return [sx0, sy0, sx1, sy1]


def map_box_src_to_preview(box, src_w: int = CAP_W, src_h: int = CAP_H):
    """Map a source-space box to the 800x450 preview (simple scale; the
    preview is the source resized to fit, aspect ~16:9 == CAP aspect)."""
    x0, y0, x1, y1 = box
    sx = PREVIEW_W / float(src_w)
    sy = PREVIEW_H / float(src_h)
    return [x0 * sx, y0 * sy, x1 * sx, y1 * sy]


def to_preview(rgb):
    """Resize any RGB numpy frame to the 800x450 preview (RGB uint8)."""
    arr = np.ascontiguousarray(rgb)
    img = Image.fromarray(arr[:, :, :3].astype(np.uint8), "RGB")
    if img.size != (PREVIEW_W, PREVIEW_H):
        img = img.resize((PREVIEW_W, PREVIEW_H), Image.BILINEAR)
    return np.asarray(img)


# --------------------------------------------------------------------------- #
# overlay
# --------------------------------------------------------------------------- #
def draw_overlay(preview_rgb, detections, badge_text=None):
    """Draw detection boxes + labels (and an optional amber DEMO badge) onto an
    800x450 RGB preview. `detections` boxes are in PREVIEW space. Returns an
    800x450 RGB numpy array. Never raises."""
    try:
        arr = np.ascontiguousarray(preview_rgb)[:, :, :3].astype(np.uint8)
        img = Image.fromarray(arr, "RGB")
        if img.size != (PREVIEW_W, PREVIEW_H):
            img = img.resize((PREVIEW_W, PREVIEW_H), Image.BILINEAR)
    except Exception:
        img = Image.new("RGB", (PREVIEW_W, PREVIEW_H), DARK)

    draw = ImageDraw.Draw(img)
    lfont = _font(15)

    for det in detections or []:
        try:
            x0, y0, x1, y1 = det["box"]
            cls = str(det.get("cls", "obj"))
            conf = float(det.get("conf", 0.0))
        except Exception:
            continue
        x0 = max(0, min(PREVIEW_W - 1, int(round(x0))))
        y0 = max(0, min(PREVIEW_H - 1, int(round(y0))))
        x1 = max(0, min(PREVIEW_W - 1, int(round(x1))))
        y1 = max(0, min(PREVIEW_H - 1, int(round(y1))))
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle([x0, y0, x1, y1], outline=ACCENT, width=2)
        label = "%s %d%%" % (cls, int(round(conf * 100)))
        try:
            tb = draw.textbbox((0, 0), label, font=lfont)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except Exception:
            tw, th = len(label) * 8, 14
        ly = max(0, y0 - th - 6)
        draw.rectangle([x0, ly, x0 + tw + 10, ly + th + 6], fill=ACCENT)
        draw.text((x0 + 5, ly + 2), label, font=lfont, fill=TEXT)

    if badge_text:
        bfont = _font(18)
        txt = str(badge_text)
        try:
            tb = draw.textbbox((0, 0), txt, font=bfont)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except Exception:
            tw, th = len(txt) * 10, 18
        pad = 8
        draw.rectangle([10, 10, 10 + tw + pad * 2, 10 + th + pad * 2],
                       fill=AMBER)
        draw.text((10 + pad, 10 + pad - 2), txt, font=bfont, fill=DARK)

    return np.asarray(img)
