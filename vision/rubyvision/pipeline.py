"""rubyvision capture -> inference -> annotate -> publish loop.

A capture thread owns the active source and keeps only the LATEST frame in a
locked slot (drop-old policy), so inference never blocks the camera. The main
loop letterboxes that frame, runs the detector, maps boxes to the 800x450
preview, annotates, and publishes frame + status. status.json is written at
>= 2 Hz even when capture stalls (a heartbeat tick), so the HUD always sees a
fresh timestamp or correctly flips to OFFLINE.

cmd.json is polled each loop: "cycle_source" reopens the next source kind,
"cycle_model" advances the detector across discovered HEFs (and the stub).

SIGTERM/SIGINT shut the loop down cleanly.
"""

from __future__ import annotations

import os
import signal
import threading
import time

from . import annotate, detector, sources
from .publisher import Publisher

SOC_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"


def _soc_temp_c():
    try:
        with open(SOC_TEMP_PATH) as fh:
            return int(fh.read().strip()) / 1000.0
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# capture thread: latest-frame slot
# --------------------------------------------------------------------------- #
class Capture:
    """Background thread pulling frames from `source` into a latest-only slot.

    The source object is swappable under lock (cycle_source) without tearing
    down the thread.
    """

    def __init__(self, source, kind: str):
        self._lock = threading.Lock()
        self._source = source
        self.kind = kind
        self._frame = None
        self._frame_seq = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                src = self._source
            try:
                frame = src.read() if src is not None else None
            except Exception:
                frame = None
            if frame is not None:
                with self._lock:
                    self._frame = frame
                    self._frame_seq += 1
            else:
                # No frame this tick (paced source or transient miss).
                time.sleep(0.005)

    def latest(self):
        """Return (frame_or_None, seq). frame is a reference; treat read-only.
        Also returns the live source object for `.truth` access by the stub."""
        with self._lock:
            return self._frame, self._frame_seq, self._source

    def swap_source(self, source, kind: str):
        old = None
        with self._lock:
            old = self._source
            self._source = source
            self.kind = kind
            self._frame = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            src = self._source
        if src is not None:
            try:
                src.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #
class Pipeline:
    def __init__(self, models_dir: str, shm_dir: str, fps: float,
                 source_pref: str, detector_pref: str):
        self.models_dir = models_dir
        self.fps = max(1.0, float(fps))
        self.period = 1.0 / self.fps
        self.source_pref = source_pref
        self.detector_pref = detector_pref

        self.pub = Publisher(shm_dir)
        self._stop = threading.Event()
        self.seq = 0
        self._last_fseq = -1        # capture seq of the last fully-processed frame
        self._last_dets = []        # detections from that frame (for heartbeats)
        self._no_frame_grace = 3.0  # s with zero frames before 'starting'->'no_camera'

        # source cycling state
        self._src_cycle = list(sources.SOURCE_CYCLE)
        self._src_cycle_idx = 0

        # detector cycling state: discovered HEFs + the stub sentinel.
        self._models = detector.find_hefs(models_dir) + [""]  # "" == stub
        self._model_idx = 0

        # fps tracking
        self._infer_times = []
        self._loop_times = []

        # open source + detector
        src, kind = sources.open_source(source_pref, models_dir)
        self.capture = Capture(src, kind)
        self._init_detector(src)

    # --- setup helpers -----------------------------------------------------
    def _init_detector(self, source):
        if self.detector_pref == "stub":
            self.detector = detector.StubDetector(source)
            self.mode = "stub"
            self.model_name = "stub"
            return
        det, mode = detector.open_detector(self.detector_pref,
                                            self.models_dir, source)
        self.detector = det
        self.mode = mode
        self.model_name = getattr(det, "name", mode)
        # Align cycle index to the chosen model if it is a HEF.
        if mode == "hailo":
            hef = getattr(det, "_hef_path", "")
            if hef in self._models:
                self._model_idx = self._models.index(hef)

    # --- cmd handling ------------------------------------------------------
    def _cycle_source(self):
        self._src_cycle_idx = (self._src_cycle_idx + 1) % len(self._src_cycle)
        kind = self._src_cycle[self._src_cycle_idx]
        src, real_kind = sources.open_source(kind, self.models_dir)
        self.capture.swap_source(src, real_kind)
        # Re-point the stub at the new source (so .truth tracking follows).
        if self.mode == "stub":
            self.detector = detector.StubDetector(src)

    def _set_source(self, kind):
        """Open a specific source kind live (CONFIGURE camera selector)."""
        if not kind:
            return
        src, real_kind = sources.open_source(kind, self.models_dir)
        self.capture.swap_source(src, real_kind)
        # Keep the cycle index aligned so a later cycle_source continues sanely.
        base = "".join(c for c in real_kind if not c.isdigit())  # usb8 -> usb
        if base in self._src_cycle:
            self._src_cycle_idx = self._src_cycle.index(base)
        if self.mode == "stub":
            self.detector = detector.StubDetector(src)

    def _cycle_model(self):
        if len(self._models) <= 1:
            return  # only the stub available
        self._model_idx = (self._model_idx + 1) % len(self._models)
        target = self._models[self._model_idx]
        try:
            self.detector.close()
        except Exception:
            pass
        _, _, src = self.capture.latest()
        if not target:
            self.detector = detector.StubDetector(src)
            self.mode = "stub"
            self.model_name = "stub"
            return
        try:
            det = detector.HailoDetector(target, model_dir=self.models_dir)
            self.detector = det
            self.mode = "hailo"
            self.model_name = det.name
        except Exception:
            self.detector = detector.StubDetector(src)
            self.mode = "stub"
            self.model_name = "stub"

    def _handle_cmd(self, cmd):
        if not isinstance(cmd, dict):
            return
        action = cmd.get("cmd")
        if action == "cycle_source":
            try:
                self._cycle_source()
            except Exception:
                pass
        elif action == "cycle_model":
            try:
                self._cycle_model()
            except Exception:
                pass
        elif action == "set_source":
            try:
                self._set_source(cmd.get("source"))
            except Exception:
                pass

    # --- fps helpers -------------------------------------------------------
    @staticmethod
    def _fps_from(samples):
        if len(samples) < 2:
            return 0.0
        span = samples[-1] - samples[0]
        if span <= 0:
            return 0.0
        return (len(samples) - 1) / span

    def _push_time(self, lst, t):
        lst.append(t)
        if len(lst) > 30:
            del lst[0]

    # --- badge logic -------------------------------------------------------
    def _badge(self, kind: str, state: str):
        """DEMO badge text or None. Real detection only when running Hailo on a
        live camera (csi/usb)."""
        if state == "no_camera":
            return "DEMO - NO CAMERA"
        is_live_cam = kind.startswith("csi") or kind.startswith("usb")
        if self.mode != "hailo":
            return "DEMO - CPU STUB"
        if not is_live_cam:
            # Hailo on a pattern/video feed: real inference, synthetic input.
            return "DEMO - %s" % (kind.upper())
        return None

    # --- main loop ---------------------------------------------------------
    def run(self):
        self.capture.start()
        last_status = 0.0
        t_start = time.monotonic()
        # Initial 'starting' status so the HUD doesn't show OFFLINE on boot.
        self._publish_status(state="starting", kind=self.capture.kind,
                             detections=[], inf_fps=0.0, loop_fps=0.0,
                             hailo_temp=None)

        while not self._stop.is_set():
            t0 = time.monotonic()

            # cmd poll
            cmd = self.pub.read_cmd()
            if cmd is not None:
                self._handle_cmd(cmd)

            frame, fseq, src = self.capture.latest()
            kind = self.capture.kind

            detections = []
            inf_fps = self._fps_from(self._infer_times)
            loop_fps = self._fps_from(self._loop_times)

            # Boot-race recovery: if auto-selection landed on the stub
            # (e.g. /dev/hailo0 not ready at service start), retry the real
            # detector every 30s once the device shows up.
            now_m = time.monotonic()
            if (getattr(self.detector, "name", "") == "stub"
                    and self.detector_pref in ("auto", "hailo")
                    and now_m - getattr(self, "_hailo_retry_t", 0.0) > 30.0
                    and os.path.exists("/dev/hailo0")):
                self._hailo_retry_t = now_m
                hefs = detector.find_hefs(self.models_dir)
                if hefs:
                    try:
                        det = detector.HailoDetector(hefs[0],
                                                     model_dir=self.models_dir)
                        old = self.detector
                        self.detector = det
                        try:
                            old.close()
                        except Exception:
                            pass
                    except Exception:
                        pass

            if frame is not None and fseq != self._last_fseq:
                # New frame: letterbox -> infer -> map -> annotate -> publish.
                self._last_fseq = fseq
                img640, scale, padx, pady = annotate.letterbox(frame)
                try:
                    raw_dets = self.detector.infer(img640)
                except Exception:
                    raw_dets = []
                self._push_time(self._infer_times, time.monotonic())
                inf_fps = self._fps_from(self._infer_times)

                src_h, src_w = frame.shape[0], frame.shape[1]
                for d in raw_dets:
                    try:
                        sbox = annotate.map_box_640_to_src(
                            d["box"], scale, padx, pady, src_w, src_h)
                        pbox = annotate.map_box_src_to_preview(
                            sbox, src_w, src_h)
                        detections.append({"cls": d.get("cls", "obj"),
                                           "conf": float(d.get("conf", 0.0)),
                                           "box": [round(v, 1) for v in pbox]})
                    except Exception:
                        continue

                state = "ok"
                badge = self._badge(kind, state)
                preview = annotate.to_preview(frame)
                annotated = annotate.draw_overlay(preview, detections, badge)
                self.seq += 1
                self._last_dets = detections
                self.pub.write_frame(annotated)
                self._publish_status(state=state, kind=kind,
                                     detections=detections, inf_fps=inf_fps,
                                     loop_fps=loop_fps,
                                     hailo_temp=self._hailo_temp())
                last_status = time.monotonic()
            elif frame is not None:
                # Same frame still in the slot (loop outran the source): skip the
                # redundant infer/annotate/write_frame. Just emit the periodic
                # heartbeat (bumping ts) so the HUD stays online and re-uses the
                # already-published frame.seq (no re-decode on the HUD side).
                now = time.monotonic()
                if now - last_status >= 0.4:  # >= 2 Hz heartbeat
                    self._publish_status(state="ok", kind=kind,
                                         detections=self._last_dets,
                                         inf_fps=inf_fps, loop_fps=loop_fps,
                                         hailo_temp=self._hailo_temp())
                    last_status = now
            else:
                # No frame yet: heartbeat status so HUD timestamp stays fresh.
                # Hold 'starting' only briefly; if no frame ever arrives (wedged
                # camera), flip to 'no_camera' so the HUD shows the defined
                # 'DEMO - NO CAMERA' state rather than a perpetual 'starting'.
                if self.seq > 0:
                    state = "no_camera"
                elif (time.monotonic() - t_start) > self._no_frame_grace:
                    state = "no_camera"
                else:
                    state = "starting"
                now = time.monotonic()
                if now - last_status >= 0.4:  # >= 2 Hz heartbeat
                    self._publish_status(state=state, kind=kind,
                                         detections=[], inf_fps=inf_fps,
                                         loop_fps=loop_fps,
                                         hailo_temp=self._hailo_temp())
                    last_status = now

            self._push_time(self._loop_times, time.monotonic())

            # pace
            dt = time.monotonic() - t0
            if dt < self.period:
                self._stop.wait(self.period - dt)

        self._shutdown()

    def _hailo_temp(self):
        # The Hailo die-temperature read is a synchronous device-control
        # round-trip to the NPU (~60 ms) that contends with inference -- calling
        # it once per frame was the dominant pipeline cost (it dwarfed infer
        # itself). The HUD only needs the temperature at ~1 Hz, so cache it and
        # refresh at most once per second. This is the single biggest FPS win.
        if self.mode != "hailo":
            return None
        now = time.monotonic()
        if now - getattr(self, "_temp_t", 0.0) < 1.0:
            return getattr(self, "_temp_cached", None)
        self._temp_t = now
        try:
            self._temp_cached = self.detector.temp_c()
        except Exception:
            self._temp_cached = None
        return self._temp_cached

    def _publish_status(self, state, kind, detections, inf_fps, loop_fps,
                        hailo_temp):
        status = {
            "v": 1,
            "ts": time.time(),
            "seq": self.seq,
            "state": state,
            "mode": self.mode,
            "source": kind,
            "model": self.model_name,
            "inference_fps": round(float(inf_fps), 1),
            "pipeline_fps": round(float(loop_fps), 1),
            "hailo_temp_c": hailo_temp,
            "soc_temp_c": _soc_temp_c(),
            "frame": {"file": "frame.jpg", "w": 800, "h": 450,
                      "seq": self.seq},
            "detections": [
                {"cls": d["cls"], "conf": round(float(d["conf"]), 3),
                 "box": d["box"]} for d in (detections or [])
            ],
            "error": None,
        }
        self.pub.write_status(status)

    def stop(self, *_):
        self._stop.set()

    def _shutdown(self):
        try:
            self.capture.stop()
        except Exception:
            pass
        try:
            self.detector.close()
        except Exception:
            pass
        # Final status so the HUD can show a clean offline transition.
        try:
            self._publish_status(state="error", kind=self.capture.kind,
                                 detections=[], inf_fps=0.0, loop_fps=0.0,
                                 hailo_temp=None)
        except Exception:
            pass


def run_pipeline(models_dir, shm_dir, fps, source_pref, detector_pref):
    pipe = Pipeline(models_dir, shm_dir, fps, source_pref, detector_pref)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, pipe.stop)
        except Exception:
            pass
    pipe.run()
    return 0
