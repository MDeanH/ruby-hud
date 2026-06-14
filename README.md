# ruby-hud

**Purpose-built in-car platform** for a red 2017 MX-5 GT RF ("Ruby") running on a Raspberry Pi 5 16 GB + Hailo-10H. Not a generic dashboard template — reverse-engineered CAN, AI vision, satellite gauge display, and a production-minded self-update system with real rollback depth.

Framebuffer HUD (no X/Wayland), decoupled services, tmpfs IPC, and defensive "never raise on the render path" discipline throughout.

## What's in the box

| Area       | Role |
|------------|------|
| `hud/rubyhud/` | Main 1280×800 touch HUD — Pillow direct to `/dev/fb0` (tty1), evdev gestures, cached sprites + 2× supersample, Soul Red theme. |
| `hud/bin/` | Operational scripts (hud on/off, simdrive, screenshots, `ruby-update.sh`, `ruby-remote.py`). |
| `vision/`  | `rubyvision` service: camera (CSI/USB/video/pattern) → Hailo-10H (or stub) → annotated JPEG + status.json dropped to `/dev/shm/rubyvision`. 30 fps capture, 4 degraded modes, pluggable sources. |
| `sat/`     | `rubysat`: TCP (7878) + USB-CDC JSON state stream (~15 Hz) to the Qualia. Builds compact STATE from Snapshot + vision + SoC. Allow-listed control verbs back from satellite. |
| `qualia/`  | ESP32-S3 LVGL v8 firmware (`rubysat-display`): local gauge rendering (RPM arc, MPH, gear, 4 mini-bars, chips, link). Receives STATE over Wi-Fi/USB; cap-touch emits CMDs/verbs. Dual-transport auto. |
| `deploy/`  | systemd units + ~650-line root OTA handler (`ruby-updated`). A/B worktrees, tag-prune anti-spoof, 90 s health watch + NRestarts auto-rollback, self-install of units, scoped sudoers. |
| `ups/`     | `rubyups`: SunFounder PiPower 5 I2C monitor. Conservative debounce+grace state machine. Ships **disabled + dry-run** (telemetry only). Writes `/dev/shm/rubyups/status.json`. |
| `can/`     | `MX5ND_HSCAN.dbc` (berumiya) + `signals.py` (Motorola decode on-car verified IDs: 0x202 PCM, 0x420 temps, 0x9E fuel, roof/gear bits, BCM, etc.). Listen-only, 1.5 s stale, simdrive for vcan0. |
| `carplay/` | Architecture + `probe.js` (node-carplay + CarlinKit). No bridge/player yet — blocked on dongle + DRM/KMS/fb handoff while rubyhud paused. |

## Architecture at a glance (text diagram)

```
┌─────────────────────────────┐
│  rubyhud (Pillow /dev/fb0)  │  15 fps render loop, never blocks
│  - Gauges / Vehicle / System│
│  - CONFIGURE + AI VISION    │
│  - VisionClient (tmpfs)     │◄──┐
│  - updates.py (queue .req)  │───┼──► /run/ruby-update/queue/*.req
└─────────────────────────────┘   │
                                  │
┌─────────────────────────────┐   │   ┌──────────────────────────────┐
│  rubyvision (separate svc)  │   │   │  ruby-updated (root, flock)   │
│  Capture thread → Hailo     │   │   │  do_apply / do_rollback       │
│  (or Stub) → /dev/shm/      │───┘   │  stage worktree + verify      │
│   status.json + frame.jpg   │       │  setup.sh (300s) → flip symlink
└─────────────────────────────┘       │  90s health watch + auto-revert
                                      │  self_install units + prune
┌─────────────────────────────┐       └──────────────────────────────┘
│  rubysat (TCP 7878 + USB)   │◄──┐
│  build_state(Snapshot+vis)  │   │  STATE @15 Hz (newline JSON)
│  allowlist ruby_* verbs     │───┘  CMDs from Qualia → queue
└─────────────────────────────┘
               │
               ▼  (Wi-Fi or /dev/serial/by-id ESP32)
┌─────────────────────────────┐
│  Qualia ESP32-S3 (LVGL)     │  local render, NVS Wi-Fi/rot/mirror
│  rubysat-display firmware   │  touch → page/verb/ctl back
│  480x480 (see qualia/README)│
└─────────────────────────────┘

CAN (listen-only can0/vcan0) ──► DataLayer ──► Snapshot (shared)
UPS (I2C, disabled by default) ──► /dev/shm/rubyups/status.json (future HUD tile)
```

All IPC is boring (files + JSON) so you can `cat /dev/shm/...` and `tail -f /run/ruby-update/status.jsonl` on the car.

## Releases & updating

Releases are annotated tags `vX.Y.Z`. The car self-updates via the on-device machinery (no cloud, no broker). See **[docs/UPDATING.md](docs/UPDATING.md)** for the full flow (A/B worktrees, health-pending + boot_id anti-stale, prune-tags, `ruby-updated` verbs, setup-vision pinning lessons, rollback, firmware flash, etc.).

Root `VERSION` is the single source of truth (see same doc + CHANGELOG).

## Documentation

- `docs/UPDATING.md` — OTA, rollback, setup, pitfalls (newly added to close review drift).
- `qualia/README.md` — satellite hardware, protocol, build, caveats (touch orientation untested, compile-unverified in CI).
- `can/README.md` — DBC signals wired vs not on HS-CAN + on-car notes.
- `ups/` sources + forthcoming `ups/README.md` — safe defaults and arming procedure.
- `carplay/README.md` — current status (architecture only).
- Inline: module docstrings, `ruby-updated` top comments, VisionClient contract, historical incident notes in git/CHANGELOG.

The top-level README previously undersold scope (only hud/vision/deploy); this version and the new docs/ fix that.

## Philosophy (kept)

- Process boundaries clean; HUD never waits on inference or net.
- File-drop IPC (debuggable with cat).
- OTA has real depth (verify before flip, watch, multi-layer revert).
- CAN conservative (listen-only, explicit "not on HS-CAN" notes for battery voltage etc.).
- "Build B" satellite: Pi sends compact state, ESP renders locally.
- Defensive coding everywhere so the dash does not white-screen at 70 mph.

This is embedded-systems discipline applied to a hobby car, not a web app ported to a Pi.

---

*Happy to go deeper on OTA, CAN decode, vision pipeline, or the Qualia protocol.*
