# Changelog

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
