"""Object detectors for rubyvision.

A Detector maps a 640x640 RGB image to a list of detections expressed in that
640 space:

    infer(rgb640) -> [{"cls": str, "conf": float, "box": [x0, y0, x1, y1]}]
    name : str
    close()

Boxes are in 640x640 pixel coordinates; the pipeline maps them back to the
source frame and then to the 800x450 preview via annotate.map_box_*.

Implementations:
  StubDetector   -- pure numpy; tracks a PatternSource's ground-truth boxes
                    (mapped 1280x720 -> 640x640) with synthetic confidences.
                    Returns [] for any source without a `.truth` attribute.
  HailoDetector  -- hailo_platform (lazy import); real NPU inference. Raises
                    DetectorUnavailable if the SDK or HEF is missing.

hailo_platform is imported INSIDE HailoDetector so this module imports on a
bare pillow + numpy venv and open_detector() falls back to the stub.
"""

from __future__ import annotations

import math
import os
import time

# Source native capture size (PatternSource truth is in this space).
from .sources import CAP_H, CAP_W

INFER_SIZE = 640

# HAILO NMS BY CLASS output layout (verified via `hailortcli parse-hef
# yolov8m_h10.hef`: 80 classes, max 100 boxes/class, frame 160320 bytes =
# 40080 float32). Matches the ServeBot S1 worker constants.
NMS_NUM_CLASSES = 80
NMS_MAX_BOXES = 100
NMS_PER_CLASS_FLOATS = 1 + NMS_MAX_BOXES * 5      # 501
NMS_OUTPUT_FLOATS = NMS_NUM_CLASSES * NMS_PER_CLASS_FLOATS  # 40080


def _log(msg):
    """Throttle-friendly file logger (never raises)."""
    try:
        with open("/tmp/rubyvision.log", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass



class DetectorUnavailable(Exception):
    """Raised when a detector cannot be constructed (no SDK / no HEF)."""


# --------------------------------------------------------------------------- #
# StubDetector (pure numpy)
# --------------------------------------------------------------------------- #
class StubDetector:
    """CPU stub: echoes the source's ground-truth sprites as detections.

    Maps the source's `.truth` boxes (native CAP_WxCAP_H space) into the
    640x640 letterboxed inference space the same way annotate.letterbox does
    (uniform scale + centering pad), then attaches a gently oscillating
    synthetic confidence so the HUD shows live-looking numbers. Any source
    without `.truth` yields no detections.
    """

    def __init__(self, source_ref):
        self.name = "stub"
        self._src = source_ref
        self._t = 0

    def infer(self, rgb640):
        self._t += 1
        truth = list(getattr(self._src, "truth", None) or [])
        if not truth:
            return []
        # Letterbox transform CAP -> 640 (matches annotate.letterbox).
        scale = min(INFER_SIZE / float(CAP_W), INFER_SIZE / float(CAP_H))
        new_w = CAP_W * scale
        new_h = CAP_H * scale
        padx = (INFER_SIZE - new_w) / 2.0
        pady = (INFER_SIZE - new_h) / 2.0
        out = []
        for i, t in enumerate(truth):
            try:
                x0, y0, x1, y1 = t["box"]
                cls = t.get("cls", "object")
            except Exception:
                continue
            bx0 = x0 * scale + padx
            by0 = y0 * scale + pady
            bx1 = x1 * scale + padx
            by1 = y1 * scale + pady
            # Synthetic confidence: stable per-object base + small oscillation.
            base = 0.78 + 0.06 * (i % 3)
            conf = base + 0.10 * math.sin(self._t * 0.07 + i)
            conf = max(0.40, min(0.99, conf))
            out.append({"cls": cls, "conf": round(float(conf), 3),
                        "box": [float(bx0), float(by0),
                                float(bx1), float(by1)]})
        return out

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# COCO labels
# --------------------------------------------------------------------------- #
# Fallback 80-class COCO label list (used if model_dir/coco_labels.txt missing).
_COCO80 = (
    "person bicycle car motorcycle airplane bus train truck boat "
    "traffic_light fire_hydrant stop_sign parking_meter bench bird cat dog "
    "horse sheep cow elephant bear zebra giraffe backpack umbrella handbag "
    "tie suitcase frisbee skis snowboard sports_ball kite baseball_bat "
    "baseball_glove skateboard surfboard tennis_racket bottle wine_glass cup "
    "fork knife spoon bowl banana apple sandwich orange broccoli carrot "
    "hot_dog pizza donut cake chair couch potted_plant bed dining_table "
    "toilet tv laptop mouse remote keyboard cell_phone microwave oven toaster "
    "sink refrigerator book clock vase scissors teddy_bear hair_drier "
    "toothbrush"
).split()


def load_labels(model_dir: str):
    """Read model_dir/coco_labels.txt (one label per line) or fall back to the
    built-in 80-class COCO list. Never raises."""
    path = os.path.join(model_dir or "", "coco_labels.txt")
    try:
        with open(path, "r") as fh:
            labels = [ln.strip() for ln in fh if ln.strip()]
        if labels:
            return labels
    except Exception:
        pass
    return list(_COCO80)


# --------------------------------------------------------------------------- #
# HailoDetector (hailo_platform, lazy import)
# --------------------------------------------------------------------------- #
class HailoDetector:
    """Hailo-8/8L (H10 module) detector via hailo_platform.

    Lifecycle: VDevice -> create_infer_model(hef) -> configure -> run on a
    letterboxed 640x640 input. Parses an NMS-baked output into
    (cls, conf, box-in-640) detections.

    Raises DetectorUnavailable if hailo_platform import, device init, or the
    HEF file are unavailable.

    !!! HEF OUTPUT-TENSOR PARSING -- MUST BE VERIFIED ON THE PI !!!
    --------------------------------------------------------------------------
    The exact output-tensor layout of a HEF is model- and compile-specific and
    CANNOT be confirmed on the build host (no Hailo SDK, no device, no HEF).
    Before trusting these boxes on the Pi:

        hailortcli parse-hef /home/michael/vision/models/<model>.hef

    and inspect the output vstream(s): name, shape, format, and whether NMS
    post-processing is baked in.

    _parse_nms() below is a BEST-EFFORT parser for the common Hailo
    "nms_postprocess" output, which is delivered as a Python list with one
    entry per class; each entry is an (N, 5) float array of
    [y0, x0, y1, x1, score] with coordinates NORMALIZED to 0..1. It normalizes
    those to 640-space pixel boxes. If parse-hef shows a different layout
    (e.g. raw YOLO grids needing host-side decode + NMS, or [x0,y0,x1,y1,score,
    class] rows), REPLACE _parse_nms accordingly.
    --------------------------------------------------------------------------
    """

    def __init__(self, hef_path: str, model_dir: str = "",
                 score_thresh: float = 0.35):
        self.name = "hailo"
        self._hef_path = str(hef_path)
        self._score_thresh = float(score_thresh)
        self.labels = load_labels(model_dir or os.path.dirname(self._hef_path))

        if not os.path.isfile(self._hef_path):
            raise DetectorUnavailable("HEF not found: %s" % self._hef_path)
        try:
            import hailo_platform as hpf  # lazy
        except Exception as exc:
            raise DetectorUnavailable("hailo_platform import failed: %s" % exc)
        self._hpf = hpf

        try:
            import numpy as np
            # VDevice + InferModel exactly as the PROVEN ServeBot S1 worker
            # (same Hailo-10H, same yolov8 H10 HEF, HailoRT 5.x):
            #   ~/Desktop/mac-backup-projects-20260511/Servebot v5/
            #     hailo_detection/hailo_worker.py
            try:
                params = hpf.VDevice.create_params()
                params.scheduling_algorithm = \
                    hpf.HailoSchedulingAlgorithm.ROUND_ROBIN
                self._vdevice = hpf.VDevice(params)
            except Exception:
                self._vdevice = hpf.VDevice()
            self._infer_model = self._vdevice.create_infer_model(self._hef_path)
            try:
                self._infer_model.set_batch_size(1)
            except Exception:
                pass
            # Input stays UINT8 (per parse-hef); output FLOAT32 so the NMS
            # buffer arrives dequantized.
            try:
                self._infer_model.output().set_format_type(
                    hpf.FormatType.FLOAT32)
            except Exception:
                pass
            self._configured = self._infer_model.configure()
            # HailoRT 5.x REQUIRES a pre-allocated output buffer bound by name
            # in create_bindings(); a bare create_bindings() makes run() raise
            # HailoRTInvalidOperationException (the bug that produced silent
            # zero-detection behavior here before).
            self._output_name = self._infer_model.output().name
            self._output_buf = np.empty(NMS_OUTPUT_FLOATS, dtype=np.float32)
        except Exception as exc:
            self.close()
            raise DetectorUnavailable("hailo device/model init failed: %s"
                                      % exc)
        self.name = "hailo:" + os.path.splitext(
            os.path.basename(self._hef_path))[0]

    def infer(self, rgb640):
        """Run inference on a 640x640 RGB uint8 array; return detections in
        640 space. Never raises (returns [] on any runtime error, but logs
        the first error per minute so failures are not silent)."""
        try:
            import numpy as np
            arr = np.ascontiguousarray(rgb640, dtype=np.uint8)
            bindings = self._configured.create_bindings(
                output_buffers={self._output_name: self._output_buf})
            bindings.input().set_buffer(arr)
            self._configured.run([bindings], 2000)
            flat = self._output_buf.reshape(-1)
            return self._parse_nms_by_class(flat)
        except Exception as exc:
            now = time.monotonic()
            if now - getattr(self, "_last_err_log", 0.0) > 60.0:
                self._last_err_log = now
                _log("hailo infer failed: %r" % (exc,))
            return []

    def _parse_nms_by_class(self, flat):
        """Decode the HAILO NMS BY CLASS flat float32 buffer (verified layout:
        80 classes x [count, count*(y_min,x_min,y_max,x_max,score)...] padded
        to 100 boxes/class = 40080 floats; coords normalized 0..1). Returns
        detections in 640x640 pixel space. Ported from the proven ServeBot
        parser."""
        dets = []
        if flat.size != NMS_OUTPUT_FLOATS:
            return self._parse_nms({"out": flat})   # legacy fallback paths
        arr = flat.reshape(NMS_NUM_CLASSES, NMS_PER_CLASS_FLOATS)
        for cls_idx in range(NMS_NUM_CLASSES):
            count = int(arr[cls_idx, 0])
            if count <= 0 or count > NMS_MAX_BOXES:
                continue
            rows = arr[cls_idx, 1:1 + count * 5].reshape(count, 5)
            for row in rows:
                self._append_row(dets, cls_idx, row)
        return dets

    def _parse_nms(self, raw_outputs):
        """BEST-EFFORT parser for Hailo 'nms_postprocess' output.

        See the class docstring: VERIFY against `hailortcli parse-hef` output.
        Expects (most common case) a single output that is a list with one
        (N, 5) [y0, x0, y1, x1, score] float array per class, coords in 0..1.
        Returns detections in 640x640 pixel space.
        """
        dets = []
        try:
            # Take the first (typically only) output.
            out = next(iter(raw_outputs.values()))
        except Exception:
            return dets

        try:
            # Layout A: list/sequence of per-class arrays.
            if isinstance(out, (list, tuple)):
                per_class = out
                for cls_idx, arr in enumerate(per_class):
                    if arr is None or len(arr) == 0:
                        continue
                    for row in arr:
                        self._append_row(dets, cls_idx, row)
                return dets

            # Layout B: numpy ndarray. Could be (num_classes, N, 5) or (M, 6).
            import numpy as np
            a = np.asarray(out)
            if a.ndim == 3 and a.shape[-1] == 5:
                for cls_idx in range(a.shape[0]):
                    for row in a[cls_idx]:
                        self._append_row(dets, cls_idx, row)
            elif a.ndim == 2 and a.shape[-1] >= 6:
                # rows of [y0, x0, y1, x1, score, class] (or similar)
                for row in a:
                    cls_idx = int(row[5])
                    self._append_row(dets, cls_idx, row[:5])
            elif a.ndim == 2 and a.shape[-1] == 5:
                for row in a:
                    self._append_row(dets, 0, row)
        except Exception:
            return dets
        return dets

    def _append_row(self, dets, cls_idx, row):
        """Append one parsed [y0, x0, y1, x1, score] (normalized 0..1) row."""
        try:
            y0, x0, y1, x1, score = (float(row[0]), float(row[1]),
                                     float(row[2]), float(row[3]),
                                     float(row[4]))
        except Exception:
            return
        if score < self._score_thresh:
            return
        # Normalized -> 640-space pixels.
        px0 = max(0.0, min(1.0, x0)) * INFER_SIZE
        py0 = max(0.0, min(1.0, y0)) * INFER_SIZE
        px1 = max(0.0, min(1.0, x1)) * INFER_SIZE
        py1 = max(0.0, min(1.0, y1)) * INFER_SIZE
        if px1 <= px0 or py1 <= py0:
            return
        try:
            cls = self.labels[cls_idx]
        except Exception:
            cls = "cls%d" % int(cls_idx)
        dets.append({"cls": cls, "conf": round(score, 3),
                     "box": [px0, py0, px1, py1]})

    def temp_c(self):
        """Best-effort Hailo die temperature via the device control API.

        Returns float Celsius or None. The exact API varies by SDK version, so
        this tries a couple of shapes and otherwise returns None (the pipeline
        treats None as 'not available')."""
        try:
            dev = None
            try:
                phys = self._vdevice.get_physical_devices()
                dev = phys[0] if phys else None
            except Exception:
                dev = None
            if dev is None:
                return None
            for getter in ("get_chip_temperature", "control"):
                try:
                    if getter == "get_chip_temperature":
                        info = dev.get_chip_temperature()
                    else:
                        info = dev.control.get_chip_temperature()
                    # info may expose .ts0_temperature / .ts1_temperature.
                    vals = []
                    for attr in ("ts0_temperature", "ts1_temperature"):
                        v = getattr(info, attr, None)
                        if v is not None:
                            vals.append(float(v))
                    if vals:
                        return max(vals)
                    if isinstance(info, (int, float)):
                        return float(info)
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def close(self):
        for attr in ("_configured", "_infer_model", "_vdevice"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            for meth in ("shutdown", "release", "close", "__exit__"):
                try:
                    fn = getattr(obj, meth, None)
                    if fn is None:
                        continue
                    if meth == "__exit__":
                        fn(None, None, None)
                    else:
                        fn()
                    break
                except Exception:
                    continue
            setattr(self, attr, None)


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
# Cycle order for the HUD's cmd "cycle_model": iterate the .hef files found in
# model_dir, then the stub. find_hefs() exposes that list to the pipeline.
def find_hefs(model_dir: str):
    """Sorted list of *.hef paths in model_dir (empty if none)."""
    import glob
    try:
        return sorted(glob.glob(os.path.join(model_dir or "", "*.hef")))
    except Exception:
        return []


def open_detector(pref: str = "auto", model_dir: str = "", source=None,
                  hef_path: str = ""):
    """Open a detector. Returns (detector, mode) where mode in {"hailo",
    "stub"}.

    pref="auto": use Hailo if a HEF exists in model_dir and the SDK loads,
    else the stub. pref="stub" forces the stub. pref="hailo" tries Hailo and
    falls back to the stub on failure. hef_path overrides model_dir discovery.
    """
    pref = (pref or "auto").lower()
    if pref != "stub":
        target = hef_path
        if not target:
            hefs = find_hefs(model_dir)
            target = hefs[0] if hefs else ""
        if target:
            try:
                det = HailoDetector(target, model_dir=model_dir)
                return det, "hailo"
            except DetectorUnavailable:
                pass
            except Exception:
                pass
        # pref=="hailo" but unavailable -> fall through to stub.
    return StubDetector(source), "stub"
