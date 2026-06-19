"""AirPlay screen-mirror mode for the 7" dash.

Runs UxPlay full-screen on the 7" (kmssink needs DRM master, so rubymirror.service
runs this as root). The dash (rubyhud) is stopped by the unit Conflict while we
mirror; we hand the screen back automatically by queueing 'switch-hud' for
ruby-updated (which restarts rubyhud, and the Conflict stops us) -- either when
the phone stops AirPlay (after a short grace) or if nobody ever connects (a
timeout). So the phone is the remote: start mirroring to take the dash, stop to
give it back. No touch handling is needed (the dash isn't running to read it).

Video is letterboxed to the 1024x600 panel (whole screen, no crop; landscape
fills it, portrait is a centred strip -- a tall phone on a wide screen). Audio ->
the 3.5mm jack (card 0) for the car AUX; the panel's HDMI speakers are too weak.
See deploy/rubymirror.service + the 'mirror' verb in deploy/ruby-updated.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

NAME = "Ruby"
_AUDIO = ("volume volume=1.0 ! audioconvert ! audioresample ! "
          "alsasink device=plughw:0,0")
_VIDEO = ("videoscale add-borders=true ! video/x-raw,width=1024,height=600 ! "
          "kmssink driver-name=vc4 can-scale=false")

CONNECT_TIMEOUT = 120.0    # nobody ever mirrored -> give the dash back
DISCONNECT_GRACE = 8.0     # mirrored then stopped, no reconnect -> give it back


def _log(msg: str) -> None:
    sys.stderr.write("rubymirror: %s\n" % msg)
    sys.stderr.flush()


def _set_jack_full() -> None:
    # HDMI audio has no mixer; the 3.5mm jack does -- open it up (the car amp
    # sets the actual loudness). Best-effort; control names vary by kernel.
    for ctl in ("PCM", "Headphone"):
        subprocess.run(["amixer", "-c", "0", "sset", ctl, "100%", "unmute"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _return_to_dash() -> None:
    try:
        from . import updates
        updates.request("switch-hud")
        _log("queued switch-hud (return to dash)")
    except Exception as exc:            # must never wedge mirror mode
        _log("switch-hud request failed: %s" % exc)


def main() -> None:
    _set_jack_full()
    cmd = ["/usr/bin/uxplay", "-n", NAME, "-nh", "-s", "1024x600",
           "-as", _AUDIO, "-vs", _VIDEO]
    _log("launching uxplay")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    st = {"streaming": False, "ever": False, "drop": None}
    lock = threading.Lock()

    def _reader():
        for line in proc.stdout:                 # blocks until uxplay EOF
            low = line.lower()
            if "begin streaming" in low:
                with lock:
                    st["streaming"], st["ever"], st["drop"] = True, True, None
                _log("mirror session started")
            elif "removing connection" in low or "connection closed" in low:
                with lock:
                    if st["streaming"]:
                        st["streaming"] = False
                        st["drop"] = time.monotonic()
                        _log("mirror session ended")

    threading.Thread(target=_reader, daemon=True).start()

    start = time.monotonic()
    reason = None
    while reason is None:
        time.sleep(1.0)
        if proc.poll() is not None:
            reason = "uxplay exited (rc=%s)" % proc.returncode
            break
        now = time.monotonic()
        with lock:
            ever, streaming, drop = st["ever"], st["streaming"], st["drop"]
        if not ever and now - start > CONNECT_TIMEOUT:
            reason = "no one connected in %ds" % int(CONNECT_TIMEOUT)
        elif (not streaming) and drop and now - drop > DISCONNECT_GRACE:
            reason = "phone stopped mirroring"

    _log("returning to dash: %s" % reason)
    _return_to_dash()
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    main()
