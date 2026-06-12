"""Standalone per-stage profiler for the rubyvision pipeline.

Imports the REAL source modules and times each stage on real camera frames,
matching the exact call sequence in pipeline.py run(). Run with the service
STOPPED so it doesn't fight for the NPU. Prints ms/stage and derived fps.

Usage:
    PYTHONPATH=<live vision dir> /home/michael/ruby-env/bin/python \
        profile_pipeline.py --models /home/michael/vision/models --secs 10
"""
import argparse
import io
import os
import time

import numpy as np
from PIL import Image

from rubyvision import annotate, detector, sources


def pct(times, p):
    if not times:
        return 0.0
    s = sorted(times)
    i = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[i] * 1000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="/home/michael/vision/models")
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--source", default="csi")
    args = ap.parse_args()

    src, kind = sources.open_source(args.source, args.models)
    print("source:", kind)
    det, mode = detector.open_detector("auto", args.models, src)
    print("detector mode:", mode, "name:", getattr(det, "name", "?"))

    # warm camera
    t_warm = time.monotonic()
    while time.monotonic() - t_warm < 1.5:
        src.read()

    stages = {k: [] for k in
              ("capture", "letterbox", "infer", "map_annotate",
               "encode_pil", "encode_cv2", "publish_fsync", "total")}
    ndet = 0
    nframes = 0
    last_box = None

    # temp publish target (tmpfs)
    out_path = "/dev/shm/rubyvision_prof.jpg"
    out_tmp = out_path + ".tmp"

    t_end = time.monotonic() + args.secs
    while time.monotonic() < t_end:
        ttot0 = time.monotonic()

        t0 = time.monotonic()
        frame = src.read()
        if frame is None:
            time.sleep(0.002)
            continue
        stages["capture"].append(time.monotonic() - t0)

        t0 = time.monotonic()
        img640, scale, padx, pady = annotate.letterbox(frame)
        stages["letterbox"].append(time.monotonic() - t0)

        t0 = time.monotonic()
        raw = det.infer(img640)
        stages["infer"].append(time.monotonic() - t0)
        ndet += len(raw)
        if raw:
            last_box = raw[0]

        t0 = time.monotonic()
        src_h, src_w = frame.shape[0], frame.shape[1]
        dets = []
        for d in raw:
            sbox = annotate.map_box_640_to_src(d["box"], scale, padx, pady,
                                               src_w, src_h)
            pbox = annotate.map_box_src_to_preview(sbox, src_w, src_h)
            dets.append({"cls": d.get("cls", "obj"),
                         "conf": float(d.get("conf", 0.0)),
                         "box": [round(v, 1) for v in pbox]})
        preview = annotate.to_preview(frame)
        annotated = annotate.draw_overlay(preview, dets, None)
        stages["map_annotate"].append(time.monotonic() - t0)

        # encode via PIL (current path)
        t0 = time.monotonic()
        arr = np.ascontiguousarray(annotated)[:, :, :3].astype(np.uint8)
        pimg = Image.fromarray(arr, "RGB")
        buf = io.BytesIO()
        pimg.save(buf, format="JPEG", quality=80)
        data_pil = buf.getvalue()
        stages["encode_pil"].append(time.monotonic() - t0)

        # encode via cv2 (candidate) -- cv2 expects BGR
        t0 = time.monotonic()
        import cv2
        bgr = cv2.cvtColor(annotated, cv2.COLOR_RGB2BGR)
        ok, enc = cv2.imencode(".jpg", bgr,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        data_cv2 = enc.tobytes() if ok else b""
        stages["encode_cv2"].append(time.monotonic() - t0)

        # publish (write + fsync + replace), as Publisher does
        t0 = time.monotonic()
        with open(out_tmp, "wb") as fh:
            fh.write(data_pil)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(out_tmp, out_path)
        stages["publish_fsync"].append(time.monotonic() - t0)

        stages["total"].append(time.monotonic() - ttot0)
        nframes += 1

    try:
        src.close()
    except Exception:
        pass
    try:
        det.close()
    except Exception:
        pass

    print("\nframes=%d  total_dets=%d  last_det=%r  pil_jpg=%dB  cv2_jpg=%dB"
          % (nframes, ndet, last_box, len(data_pil), len(data_cv2)))
    print("%-16s %8s %8s %8s %8s" % ("stage", "mean", "p50", "p95", "max"))
    order = ("capture", "letterbox", "infer", "map_annotate",
             "encode_pil", "encode_cv2", "publish_fsync", "total")
    for k in order:
        v = stages[k]
        if not v:
            print("%-16s   (none)" % k)
            continue
        mean = sum(v) / len(v) * 1000.0
        print("%-16s %7.2f %7.2f %7.2f %7.2f"
              % (k, mean, pct(v, 50), pct(v, 95), pct(v, 100)))
    # Derived effective fps if total stays as PIL path vs cv2 path.
    tt = stages["total"]
    if tt:
        mean_total = sum(tt) / len(tt)
        # cv2 path = total - encode_pil + encode_cv2
        ep = sum(stages["encode_pil"]) / len(stages["encode_pil"])
        ec = sum(stages["encode_cv2"]) / len(stages["encode_cv2"])
        cv2_total = mean_total - ep + ec
        print("\nmean total/frame (PIL encode): %.2f ms -> %.1f fps"
              % (mean_total * 1000, 1.0 / mean_total))
        print("mean total/frame (cv2 encode): %.2f ms -> %.1f fps"
              % (cv2_total * 1000, 1.0 / cv2_total))


if __name__ == "__main__":
    main()
