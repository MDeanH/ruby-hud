"""Linux framebuffer blitter for rubyhud.

Targets /dev/fb0 on the Pi (vc4-kms). Reads geometry from sysfs, mmaps the
framebuffer, and packs PIL RGB images to RGB565 (bpp16) or XRGB8888 (bpp32,
byte order B,G,R,X little-endian) honoring stride.
"""

from __future__ import annotations

import mmap
import os

import numpy as np
from PIL import Image

_FB_DEV = "/dev/fb0"
_SYS_SIZE = "/sys/class/graphics/fb0/virtual_size"
_SYS_BPP = "/sys/class/graphics/fb0/bits_per_pixel"


def _read_int_pair(path):
    with open(path, "r") as fh:
        txt = fh.read().strip()
    a, b = txt.split(",")
    return int(a), int(b)


def _read_int(path):
    with open(path, "r") as fh:
        return int(fh.read().strip())


class FrameBuffer:
    def __init__(self, dev: str = _FB_DEV):
        self.dev = dev
        self.width, self.height = _read_int_pair(_SYS_SIZE)
        self.bpp = _read_int(_SYS_BPP)
        if self.bpp not in (16, 32):
            raise RuntimeError(
                "unsupported framebuffer bpp=%d (need 16 or 32)" % self.bpp
            )

        # stride: assume no row padding (true for vc4-kms); allow override.
        default_stride = self.width * self.bpp // 8
        env_stride = os.environ.get("RUBYHUD_FB_STRIDE")
        if env_stride:
            try:
                self.stride = int(env_stride)
            except ValueError:
                self.stride = default_stride
            # A stride below the packed row width is never valid and would
            # make _pack raise (broadcast error) -> silently blank screen.
            if self.stride < default_stride:
                self.stride = default_stride
        else:
            self.stride = default_stride

        self._size = self.stride * self.height
        self._fd = os.open(self.dev, os.O_RDWR)
        self._mm = mmap.mmap(self._fd, self._size,
                             mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)

    # --- packing -----------------------------------------------------------
    def _pack(self, img: Image.Image) -> np.ndarray:
        """Return an (H, row_bytes) uint8 array honoring stride, ready to write."""
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height), Image.LANCZOS)
        arr = np.asarray(img, dtype=np.uint8)  # (H, W, 3) RGB

        if self.bpp == 16:
            r = (arr[:, :, 0].astype(np.uint16) >> 3) << 11
            g = (arr[:, :, 1].astype(np.uint16) >> 2) << 5
            b = (arr[:, :, 2].astype(np.uint16) >> 3)
            packed = (r | g | b).astype("<u2")          # little-endian u16
            row_px = packed.view(np.uint8).reshape(self.height, self.width * 2)
            row_bytes = self.width * 2
        else:  # bpp == 32, XRGB8888 LE -> byte order B, G, R, X
            out = np.empty((self.height, self.width, 4), dtype=np.uint8)
            out[:, :, 0] = arr[:, :, 2]  # B
            out[:, :, 1] = arr[:, :, 1]  # G
            out[:, :, 2] = arr[:, :, 0]  # R
            out[:, :, 3] = 255           # X
            row_px = out.reshape(self.height, self.width * 4)
            row_bytes = self.width * 4

        if self.stride == row_bytes:
            return row_px
        # Pad each row out to stride.
        padded = np.zeros((self.height, self.stride), dtype=np.uint8)
        padded[:, :row_bytes] = row_px
        return padded

    def blit(self, img: Image.Image) -> None:
        """Pack and write an RGB image to the framebuffer."""
        rows = self._pack(img)  # (H, stride)
        self._mm.seek(0)
        self._mm.write(rows.tobytes())

    def clear(self, color=(0, 0, 0)) -> None:
        img = Image.new("RGB", (self.width, self.height), tuple(color))
        self.blit(img)

    def close(self) -> None:
        try:
            if getattr(self, "_mm", None) is not None:
                self._mm.flush()
                self._mm.close()
                self._mm = None
        except Exception:
            pass
        try:
            if getattr(self, "_fd", None) is not None:
                os.close(self._fd)
                self._fd = None
        except Exception:
            pass
