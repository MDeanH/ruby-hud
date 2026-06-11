"""rubyvision entrypoint: python -m rubyvision [options].

Options:
  --models DIR     model directory (HEFs + coco_labels.txt). Default
                   /home/michael/vision/models.
  --shm DIR        tmpfs output directory. Default /dev/shm/rubyvision.
  --fps N          target pipeline fps. Default 15.
  --source PREF    csi|usb|video|pattern|auto. Default auto.
  --detector PREF  hailo|stub|auto. Default auto.

The pipeline never blocks the HUD; all hardware deps are imported lazily so
this runs on a bare pillow + numpy environment (pattern + stub path).
"""

from __future__ import annotations

import argparse
import sys

from .pipeline import run_pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rubyvision",
                                 description="Ruby camera/inference service")
    ap.add_argument("--models", default="/home/michael/vision/models",
                    help="model directory (HEFs + coco_labels.txt)")
    ap.add_argument("--shm", default="/dev/shm/rubyvision",
                    help="tmpfs output directory")
    ap.add_argument("--fps", type=float, default=15.0,
                    help="target pipeline fps")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "csi", "usb", "video", "pattern"],
                    help="frame source preference")
    ap.add_argument("--detector", default="auto",
                    choices=["auto", "hailo", "stub"],
                    help="detector preference")
    args = ap.parse_args(argv)
    return run_pipeline(args.models, args.shm, args.fps, args.source,
                        args.detector)


if __name__ == "__main__":
    sys.exit(main())
