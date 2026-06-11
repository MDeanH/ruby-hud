"""rubyvision -- Ruby MX-5 camera/inference service.

A standalone process (its own systemd unit) that captures camera frames, runs
object detection (Hailo when available, a pure-numpy stub otherwise), annotates
an 800x450 preview, and publishes the result to a tmpfs directory
(/dev/shm/rubyvision) as atomic file drops. The HUD's AIVisionPage reads those
drops without ever blocking on this process.

All hardware dependencies (opencv / picamera2 / hailo_platform) are imported
LAZILY inside their respective implementations, so the package imports and the
PatternSource + StubDetector path run on a bare pillow + numpy environment.
"""

__version__ = "0.1.0"
