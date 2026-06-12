# Changelog

## v3.1.2 — 2026-06-11

## v3.2.0 — 2026-06-11
- satellite: rubysat TCP state-publisher (Ruby) + Qualia ESP32-S3 LVGL firmware
  (Build B smart client). Ruby feeds live vehicle state; Qualia renders gauges
  locally, cap-touch sends commands back.
- vision: yolov8m_h10 HEF wired (real Hailo object detection, not stub).


## v3.1.3 — 2026-06-11
- vision: fix CSI camera red cast (picamera2 RGB888 is BGR byte order -> swap to RGB).

- rubyvision import robustness: unit uses PYTHONPATH=/home/michael/hud/../vision
  (live release, auto-tracks OTA) instead of a fragile pip editable install that
  could dangle at a pruned worktree. OV5647 CSI camera support verified.

## v3.1.1 — 2026-06-11
- Vision: publisher self-heals /dev/shm/rubyvision dir before each write
  (survives Debian tmpfiles cleanup of tmpfs that broke the service mid-run).

## v3.1.0 — 2026-06-11
- Self-update system: on-device updater (check/apply/rollback) from the touchscreen,
  A/B release worktrees + atomic symlink flip, three-layer crash auto-rollback,
  offline-capable rollback. Root handler triggered by a path-unit queue (no sudoers).
- Settings page: TouchMenu framework (rows/scroll/submenu/confirm modal), Check for
  updates / Update now / Rollback / Version-About / Service controls.
- AI Vision: rubyvision service (camera -> Hailo-10H -> annotated frame) decoupled from
  the HUD via tmpfs file-drop IPC; AIVisionPage with live preview + 4 degraded modes;
  pluggable sources (CSI/USB/video/pattern) + stub detector so it runs with no hardware.
- Touch: vertical swipes added (menu scroll); hold delegates to page (menu back).

## v3.0.0 — 2026-06-11
- Baseline: rubyhud v3 (touch UI, 3 pages, premium cluster visuals). First under git/OTA.
