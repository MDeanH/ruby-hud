"""Atomic file-drop IPC for rubyvision.

The service writes into a tmpfs directory (default /dev/shm/rubyvision):
    frame.jpg    annotated 800x450 RGB JPEG q80 (via .tmp + os.replace)
    status.json  service status (schema v1) written >= 2 Hz
and reads:
    cmd.json     HUD -> service commands, mtime-gated so each is consumed once.

Every method is failure-guarded (never raises into the pipeline). Errors are
logged to /tmp/rubyvision.log with throttling so a wedged disk can't spam.
"""

from __future__ import annotations

import io
import json
import os
import time

import numpy as np
from PIL import Image

_LOG = "/tmp/rubyvision.log"


class Publisher:
    def __init__(self, shm_dir: str):
        self.dir = shm_dir
        self.frame_path = os.path.join(shm_dir, "frame.jpg")
        self.frame_tmp = os.path.join(shm_dir, ".frame.jpg.tmp")
        self.status_path = os.path.join(shm_dir, "status.json")
        self.status_tmp = os.path.join(shm_dir, ".status.json.tmp")
        self.cmd_path = os.path.join(shm_dir, "cmd.json")
        self._last_cmd_mtime = 0.0
        self._last_log = 0.0
        try:
            os.makedirs(shm_dir, exist_ok=True)
        except Exception as exc:
            self._log("mkdir %s failed: %s" % (shm_dir, exc))

    # --- logging -----------------------------------------------------------
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

    # --- frame -------------------------------------------------------------
    def write_frame(self, rgb800x450) -> bool:
        """Encode the RGB array as JPEG q80 and atomically replace frame.jpg.
        Returns True on success. Never raises."""
        try:
            arr = np.ascontiguousarray(rgb800x450)[:, :, :3].astype(np.uint8)
            img = Image.fromarray(arr, "RGB")
            if img.size != (800, 450):
                img = img.resize((800, 450), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            data = buf.getvalue()
            with open(self.frame_tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(self.frame_tmp, self.frame_path)
            return True
        except Exception as exc:
            self._log("write_frame failed: %s" % exc)
            try:
                if os.path.exists(self.frame_tmp):
                    os.remove(self.frame_tmp)
            except Exception:
                pass
            return False

    # --- status ------------------------------------------------------------
    def write_status(self, status: dict) -> bool:
        """Atomically replace status.json with the given dict. Never raises."""
        try:
            # default=float coerces stray numpy scalars (np.float32, etc.) to a
            # plain number instead of raising TypeError -- a single un-cast value
            # must NOT take the whole status channel dark (HUD would show OFFLINE
            # permanently with no obvious symptom).
            data = json.dumps(status, separators=(",", ":"),
                              default=float).encode("utf-8")
            with open(self.status_tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(self.status_tmp, self.status_path)
            return True
        except Exception as exc:
            self._log("write_status failed: %s" % exc)
            try:
                if os.path.exists(self.status_tmp):
                    os.remove(self.status_tmp)
            except Exception:
                pass
            return False

    # --- cmd ---------------------------------------------------------------
    def read_cmd(self):
        """Return the cmd dict iff cmd.json changed since last read, else None.
        Never raises."""
        try:
            st = os.stat(self.cmd_path)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._log("stat cmd failed: %s" % exc)
            return None
        if st.st_mtime <= self._last_cmd_mtime:
            return None
        self._last_cmd_mtime = st.st_mtime
        try:
            with open(self.cmd_path, "r") as fh:
                return json.load(fh)
        except Exception as exc:
            self._log("read_cmd failed: %s" % exc)
            return None
