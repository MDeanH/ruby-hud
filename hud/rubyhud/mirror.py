"""AirPlay screen-mirror mode for the 7" dash.

Runs UxPlay full-screen on the 7" (kmssink needs DRM master, so rubymirror.service
runs this as root). The dash (rubyhud) is stopped by the unit Conflict while we
mirror; we hand the screen back automatically by queueing 'switch-hud' for
ruby-updated (which restarts rubyhud, and the Conflict stops us). Three ways back:
  * press-and-hold the 7" for LONG_PRESS seconds (works any time, even before a
    phone connects -- the dash isn't running, so we read the touchscreen here),
  * the phone stops AirPlay (after a short grace), or
  * nobody ever connects (a timeout).

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
LONG_PRESS = 1.5           # hold the 7" this long -> return to the dash


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


def _find_touch():
    """The touchscreen evdev node, or None. Soft-fails without python3-evdev."""
    try:
        import evdev
        from evdev import ecodes as e
    except Exception:
        return None
    for path in evdev.list_devices():
        dev = None
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            keys = set(caps.get(e.EV_KEY, []))
            ab = dict(caps.get(e.EV_ABS, []))
            mt = e.ABS_MT_POSITION_X in ab
            st = e.ABS_X in ab and e.BTN_TOUCH in keys
            if mt or st:
                return dev
            dev.close()
        except Exception:
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                pass
    return None


def _longpress_watcher(fire) -> None:
    """Call fire() when the screen is held >= LONG_PRESS s. Soft-fails when evdev
    or the touchscreen is unavailable (the phone-driven exits still work). Uses
    BTN_TOUCH (press=1/release=0) -- the ILITEK panel reports it for any contact;
    no exclusive grab, so rubyhud can reopen the device on return."""
    import select
    dev = _find_touch()
    if dev is None:
        _log("touch: no touchscreen; long-press exit disabled")
        return
    from evdev import ecodes as e
    _log("touch: long-press exit armed (%s)" % dev.path)
    pressing = None                    # monotonic time of the current press
    while True:
        try:
            r, _, _ = select.select([dev.fd], [], [], 0.2)
            if r:
                for ev in dev.read():
                    if ev.type == e.EV_KEY and ev.code == e.BTN_TOUCH:
                        pressing = time.monotonic() if ev.value else None
            if pressing and time.monotonic() - pressing >= LONG_PRESS:
                _log("touch: long-press -> return")
                fire()
                return
        except OSError:
            return                     # device unplugged
        except Exception as exc:
            _log("touch read error: %r" % exc)
            return


def main() -> None:
    _set_jack_full()
    cmd = ["/usr/bin/uxplay", "-n", NAME, "-nh", "-s", "1024x600",
           "-as", _AUDIO, "-vs", _VIDEO]
    _log("launching uxplay")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    st = {"streaming": False, "ever": False, "drop": None, "longpress": False}
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

    def _fire_longpress():
        with lock:
            st["longpress"] = True

    threading.Thread(target=_reader, daemon=True).start()
    threading.Thread(target=_longpress_watcher, args=(_fire_longpress,),
                     daemon=True).start()

    start = time.monotonic()
    reason = None
    while reason is None:
        time.sleep(0.5)
        if proc.poll() is not None:
            reason = "uxplay exited (rc=%s)" % proc.returncode
            break
        now = time.monotonic()
        with lock:
            ever, streaming = st["ever"], st["streaming"]
            drop, longpress = st["drop"], st["longpress"]
        if longpress:
            reason = "long-press"
        elif not ever and now - start > CONNECT_TIMEOUT:
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
