# AGENTS.md

## Cursor Cloud specific instructions

This repo (`ruby-hud`) targets a Raspberry Pi 5 living in a Mazda MX-5. **The
Cloud VM is NOT a Pi**: there is no framebuffer (`/dev/fb0`), no Hailo NPU, no
CAN adapter, no I2C UPS HAT, and no camera. Every Python package imports its
hardware deps lazily and degrades gracefully, so all four services run on the
bare host via their demo/stub/NO_HAT paths.

### Layout & how to run (no install step — run via `PYTHONPATH`)
Packages are not pip-installed; on the Pi they are pinned onto `PYTHONPATH`.
Run each from the repo root with its source dir on the path. `rubyhud` has **no**
`pyproject.toml` and is always run as `PYTHONPATH=/workspace/hud python3 -m rubyhud`.
The update script installs the shared deps (`pillow numpy python-can smbus2`).

- **rubyhud** (`hud/rubyhud`, the flagship 1280×800 touch HUD): normal mode opens
  `/dev/fb0` and **cannot run here** (it logs the failure and exits 1). Use the
  oneshot renderer instead — it never touches the framebuffer or touch layer:
  `cd hud && RUBYHUD_ONESHOT=1 RUBYHUD_DEMO=1 RUBYHUD_PAGE=<0-4> RUBYHUD_PNG=/tmp/hud.png python3 -m rubyhud`
  Pages: 0 GAUGES, 1 CAN BUS, 2 SYSTEM, 3 SETTINGS, 4 AI VISION.
- **rubyvision** (`vision/rubyvision`): `PYTHONPATH=/workspace/vision python3 -m rubyvision --source pattern --detector stub --shm /dev/shm/rubyvision --fps 15`
  (long-running). `auto` also lands on pattern+stub when no camera/Hailo exists.
  Its default `--shm /dev/shm/rubyvision` is exactly what HUD page 4 and rubysat
  read, so running it then re-rendering page 4 shows live annotated detections.
- **rubysat** (`sat/rubysat`, stdlib only): `PYTHONPATH=/workspace/sat python3 -m rubysat --novehicle --port 7878 --hz 15`
  (long-running TCP server). `--novehicle` serves an animated demo snapshot;
  without it the server needs an importable `rubyhud` + a real/`vcan0` CAN bus.
  Read the stream with a TCP client to `127.0.0.1:7878` (newline-delimited JSON).
- **rubyups** (`ups/rubyups`): `python3 -m rubyups read` prints a diagnostic and
  exits 2 with no HAT. Monitor loop: `python3 -m rubyups --disabled --dry-run --poll-s 1 --status-path /tmp/rubyups-status.json run`.
  With no I2C it stays in NO_HAT mode and never powers off. **Never pass
  `--live`/`--enabled` on a dev host** — those arm the real `poweroff` path.

### What you cannot exercise here
- `python -m rubyhud.simdrive` and `hud/bin/sim-up.sh` need `vcan0` via
  `modprobe vcan` + `ip link` (no iproute2 / vcan module in the VM). Use the
  `--novehicle` / `RUBYHUD_DEMO` paths instead of a CAN bus.
- Live `can0`, CSI/USB/Hailo vision, the `qualia/` ESP32 firmware (needs
  `arduino-cli`/PlatformIO + board), and everything under `deploy/`
  (`*.service`, `install.sh`, `setup*.sh`, `ruby-updated`) assume Pi hardware,
  root, systemd, and the `michael` user — do not run them on the dev host.

### Lint / test / build
There is **no automated test suite and no lint config** in this repo. The
available sanity check is byte-compilation:
`python3 -m compileall -q hud/rubyhud vision/rubyvision sat/rubysat ups/rubyups`.
There is no build step (pure Python; `qualia/` firmware builds are embedded-only).
