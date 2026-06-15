# Changelog

All notable changes. Releases are annotated tags `vX.Y.Z`. See `docs/UPDATING.md` for the OTA mechanics, rollback, and setup details. Root `VERSION` is the single source of truth (propagated to hud/, packages, firmware, and this file on cut).

## v3.15.0 — 2026-06-15
- RF roof status decode calibrated on-car (0x472): the VEHICLE roof chip now shows CLOSED / OPENING / OPEN / CLOSING correctly. byte2 high-nibble = motion direction (0 idle / 2 opening / 4 closing, +0x8 = blink phase); when idle, byte1 = resting state (0x05 closed / 0x03 open). Replaces the prior best-guess `{0:CLOSED,1:OPEN}` map. RX-only — no actuation (the ND RF roof is a hardwired switch, not CAN-commandable). The full Tesla-style BODY-page roof view ships with the door/trunk feature.
- Note: CHANGELOG entries for v3.11.1–v3.14.0 were not backfilled by their releases; see the tag messages for those.

## v3.11.0 — 2026-06-15
- WI-FI page (full-screen, deep-linked from CONFIGURE > WI-FI > MANAGE / CONNECT): scan + list nearby networks (signal bars, lock, SAVED/CONNECTED tags), connect with an on-screen WPA password keyboard, switch / disconnect / forget saved networks, one-tap iPhone hotspot. All nmcli work runs off the render thread via `wifinet`; credentials are owned by NetworkManager (never stored or logged by the HUD).
- On-screen Pillow keyboard widget (`keyboard.py`): QWERTY + symbol layer, sticky shift, hand-drawn key glyphs; hit-tested in screen px.
- Color-scheme groundwork (`theme.py`): palette reorganized as named schemes (`soul-red` default + `ion-blue`), `apply_scheme()`/`scheme_names()`; the in-HUD picker is a later release (architect-now).
- Privilege: `netdev` polkit rule (`deploy/polkit/50-ruby-netdev-nm.rules`) lets the HUD service control NetworkManager (one-time root install, not OTA-delivered).
- Fix: CONFIGURE > WI-FI SSID row now reads via nmcli (the prior `iwgetid` isn't installed on the Pi, so it always showed `--`).

## v3.10.0 — 2026-06-13
- AI VISION on/off toggle in CONFIGURE (systemctl fire-and-forget via scoped sudoers; survives mid-update HUD restarts).
- CarPlay foundation (architecture doc + `carplay/probe.js` + node-carplay notes); blocked on CarlinKit dongle + DRM/KMS handoff validation. NAVIGATION placeholder dropped.
- CONFIGURE page polish from prior review fixes (update controls ordering, hidden CAN page, playback).

## v3.9.1 — 2026-06-13
- Restore update controls (CHECK/UPDATE/ROLLBACK) to the top of the CONFIGURE page.

## v3.9.0 — 2026-06-13
- CONFIGURE promoted to a first-class swipe page (own TouchMenu root).
- CAN page hidden (deep-link only from CONFIGURE).
- In-HUD playback + recording (ffmpeg fbdev + tail of vision frame.jpg) with on_show/on_hide lifecycle.
- 5 review-driven fixes (menu, units, status, etc.).

## v3.8.0 — 2026-06-13
- CONFIGURE as hub: unit toggles (C/F, MPH/KMH persisted in ~/hud-state JSON), screen + camera recording controls.
- AI VISION source/model cycling from the page chips.

## v3.7.0 — 2026-06-13
- Satellite (Qualia) backlight toggle + 7-inch page nav commands over rubysat-ctl.json.
- 11 adversarial-review fixes (defensive paths, stale handling, menu robustness).

## v3.6.0 — 2026-06-12
- Dual-transport satellite link: USB CDC (`/dev/serial/by-id/...ESP32*`) preferred with auto-fallback to Wi-Fi TCP; `SerialStateLink` + `TcpStateServer`; freshness + link-mode NVS on the Qualia.
- rubysat STATE schema extended for vsrc/vdets/soc + transient ack for control verbs.

## v3.5.0 — 2026-06-12
- ND1 vehicle dashboard (full DBC signal set from berumiya reverse): rpm, speed, throttle, coolant, fuel, ambient (table), gear bits, roof (RF), turn, lights, parking brake, reverse.
- Fahrenheit display option + unit persistence.
- Motorola @0+ bit extraction in `signals.py` with explicit DBC sawtooth comments and sim vs live ID collision handling.

## v3.4.1 — 2026-06-12
- DBC-accurate MX-5 ND decode (researched on-car + community DBC, not guessed). `can/MX5ND_HSCAN.dbc` vendored.

## v3.4.0 — 2026-06-12
- First real MX-5 ND CAN signals decoded live on the car (0x202 PCM, 0x420 temps, 0x9E fuel, etc.). `vcan0` simdrive for bench work.

## v3.3.1 — 2026-06-12
- 7-inch (main HUD) controls the 4-inch dash HUD + windshield mirror via rubysat-ctl.

## v3.3.0 — 2026-06-12
- Satellite control surface: 3-tile Qualia UI (gauges / status / menu) in LVGL v8 on ESP32-S3.
- Remote HUD control verbs from the 4" cap-touch back to the Pi updater queue.
- rubysat TCP newline-JSON @ ~15 Hz (STATE + ack piggyback); dual client (USB/Wi-Fi).

## v3.2.6 — 2026-06-12
- Vehicles detectable: NMS-BY-CLASS output from Hailo is PACKED (`[count, count*5 floats]`) not fixed-stride 501. `_parse_nms_by_class` walk fixed (higher-class cars were previously dropped).

## v3.2.5 — 2026-06-12
- Fix vision running stale code after OTA: pin editable install (or .pth fallback) to the stable `hud-repo/vision` clone (not a release worktree that gets pruned). `deploy/setup-vision.sh` + `HUD_REPO_VISION` logic + service PYTHONPATH note. Historical symptom: dets=0 despite correct HEF (fixed 2026-06-12).

## v3.2.4 — 2026-06-12
- Vision robustness: NPU warmup + boot-race Hailo recovery (re-open detector if `/dev/hailo0` appears later); capture thread latest-frame-only slot.

## v3.2.3 — 2026-06-12
- Fix Hailo inference pipeline: ported proven ServeBot S1 worker approach (letterbox, pre-alloc output, ROUND_ROBIN, explicit chip temp).

## v3.2.2 — 2026-06-12
- Qualia display quality: bounce buffer in panel bring-up eliminates PSRAM shimmer; palette + gauge polish (pre-blur, supersample, Soul Red theme).

## v3.2.1 — 2026-06-12
- Qualia panel bring-up confirmed: TL040WVS03 (4.0" 480x480 RGB) — working display + timings. (Note: some docs and comments still referenced the marketed 720 resolution.)

## v3.2.0 — 2026-06-11
- Qualia satellite display (rubysat + ESP32-S3 LVGL firmware "Build B" — local gauge render, compact STATE JSON, not pixels).
- Real Hailo-10H object detection (yolov8m_h10.hef) with 4 degraded modes (stub, offline, no-camera, demo) and tmpfs IPC (`/dev/shm/rubyvision` status.json + frame.jpg + cmd.json).
- `vision/` package split, `rubyvision` service, `AIVisionPage`, `VisionClient` (mtime/seq gated, never blocks render).

## v3.1.3 — 2026-06-11
- Vision: fix CSI camera red cast (picamera2 RGB888 reported as BGR byte order; explicit swap to RGB in annotate path).
- rubyvision import robustness: unit uses `PYTHONPATH=/home/michael/hud/../vision` (live release, auto-tracks OTA) instead of a fragile pip editable install that could dangle at a pruned worktree after A/B flip. OV5647 CSI camera support verified on-car.

## v3.1.2 — 2026-06-11
- (Crash drill + revert recorded in history for boot-failure resilience testing.)
- rubyvision PYTHONPATH auto-track release (see v3.1.3 and v3.2.5 for the full pinning story).

## v3.1.1 — 2026-06-11
- Vision: publisher self-heals `/dev/shm/rubyvision` dir before each write (survives Debian tmpfiles cleanup of tmpfs that broke the service mid-run). Same pattern later used by rubyups `_StatusWriter`.

## v3.1.0 — 2026-06-11
- Self-update system: on-device updater (check/apply/rollback) from the touchscreen, A/B release worktrees + atomic symlink flip, three-layer crash auto-rollback (watch + health-pending + bounded restart), offline-capable manual rollback. Root handler triggered by a path-unit queue (unprivileged HUD never needs sudo for git).
- Settings page: TouchMenu framework (stack/scroll/submenu/confirm modal/zebra), CHECK FOR UPDATES / UPDATE NOW / ROLLBACK / VERSION-ABOUT / SERVICE controls. Atomic queue writes under `/run/ruby-update`.
- AI Vision: rubyvision service (camera -> Hailo-10H -> annotated frame) decoupled from the HUD via tmpfs file-drop IPC; AIVisionPage with live preview + 4 degraded modes (OFFLINE, DEMO - NO CAMERA, DEMO - CPU STUB, etc.); pluggable sources (CSI fast sensor_mode 30 fps / USB / video loop / pattern synth) + stub detector so the repo runs for UI dev with no Pi hardware.
- Touch: vertical swipes for menu scroll; hold delegates to page (menu back); 1 s RESCAN never-die thread.
- Deploy: systemd units (rubyhud TTY direct fb0, rubyvision video+render, rubysat, ruby-updated path+service, health limiter 4/120s → rollback), scoped sudoers, tmpfiles, setup.sh idempotent no-upgrade.

## v3.0.0 — 2026-06-11
- Baseline: rubyhud v3 (touch UI, 3 pages — GAUGES/VEHICLE/SYSTEM, premium cluster visuals via Pillow direct to /dev/fb0, no compositor). CAN DataLayer (listen-only, 1.5 s stale blanking, Motorola decode), gauges with cached sprites + 2× supersample + pre-Gaussian, evdev gestures, first git + OTA skeleton. Recovered from prior "ruby" tree.

---

Older tags (v2 and earlier) predate the current git history and A/B OTA discipline; they are not enumerated here. The engineering standard (never raise on render, tmpfs IPC, conservative CAN, defensive readers, A/B verified worktrees) begins with the v3 series.
