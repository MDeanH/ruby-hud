# Updating ruby-hud (OTA + manual)

This document is the single source for release, update, rollback, and operational procedures on the car. It is referenced from the root README and must be kept in sync with `deploy/ruby-updated`, the systemd units, and `deploy/setup*.sh`.

Releases are annotated tags `vX.Y.Z`. The Pi self-updates to the highest reachable `v*` tag (or an explicit ref) using an unprivileged queue + privileged root handler. The design goal is **safe A/B deploys with three-layer rollback and no requirement for the HUD code to hold root**.

## Versioning policy (single source of truth)

- `VERSION` (repo root) is canonical.
- On release cut: update root `VERSION`, sync `hud/VERSION`, `hud/rubyhud/__init__.py:__version__`, the three `pyproject.toml` package versions (sat/vision/ups), and `qualia/rubysat-display/menu_ui.cpp:FW_VERSION`.
- CHANGELOG.md must list every tag with the key changes from the commit message + any incident notes.
- Firmware and Python packages are intentionally kept in lock-step with the HUD release for a given tag.

See also the "Version chaos" section of any prior review notes; the root `VERSION` wins.

## Normal OTA flow (from the car)

1. **HUD (CONFIGURE page)**
   - CHECK FOR UPDATES → calls `rubyhud.updates.request({"cmd":"check"})` which atomically drops a `.req` file under `/run/ruby-update/queue/`.
   - UPDATE NOW (enabled only when a newer tag is reported) → same for `apply`.
   - ROLLBACK (enabled when `hud-state/previous` exists) → `rollback`.
   - These never require sudo in the HUD process; the path unit + root service do the privileged work.

2. **Satellite (Qualia 4" via rubysat)**
   - MENU → RUBY page exposes the same verbs (`ruby_check`, `ruby_update`, `ruby_rollback`, `ruby_restart_hud`, `ruby_switch_dash`).
   - rubysat allow-lists exactly these, translates to the same queue requests, and piggy-backs transient `"ack"` fields on the next few STATE lines.

3. **Remote / SSH**
   - `hud/bin/ruby-remote.py` (or manual `echo '{"cmd":"check"}' > /run/ruby-update/queue/$(date +%s%3N).req`).
   - `hud/bin/ruby-update.sh` is a thin wrapper that enqueues + watches the status file.

4. **Root handler (`ruby-updated`)**
   - Triggered by `ruby-updated.path` (PathExistsGlob on the queue) or direct `systemctl start ruby-updated.service`.
   - Always runs under flock; concurrent requests see "busy".
   - Every phase writes both `/run/ruby-update/status.jsonl` (append log) and `status.json` (latest snapshot) so the HUD and satellite can show progress without polling logs.
   - Verbs: `--consume`, `check`, `apply [ref]`, `rollback [ref]`, `auto-rollback`, `adopt <tag>`, `restart-hud`, `switch-*`.

## How `apply` actually works (A/B + verification)

(See `deploy/ruby-updated` for the authoritative implementation; this is a prose summary.)

- Preflight: >=500 MiB free on $HOME, live symlink sanity, tag format.
- `git fetch --tags --prune --prune-tags` (run as michael). Prune-tags defeats local tag spoofing: a malicious `git tag vX <bad>` on the Pi is dropped on the next fetch.
- Stage: `git worktree add --detach $RELEASES/$ref $ref`. Then `verify_release` (strict): the worktree HEAD must exactly match the tag's commit, and `git status --porcelain` must be clean. A partial or planted tree is removed and re-staged.
- Tracked-only variant of verify is used later to allow setup.sh's legitimate untracked egg-info/__pycache__ while still catching post-stage tampering of tracked files.
- `deploy/setup.sh` (300 s timeout, root): idempotent apt (no-upgrade), pip (no-upgrade), fonts, then delegates to `setup-vision.sh` + `setup-sat.sh`. Fail here aborts before the live tree is touched.
- Write `hud-state/health-pending.json` (prev, new, boot_id). This is the "in-flight" marker for auto-rollback.
- Atomic flip: `ln -sfn $RELEASES/$ref/hud $LIVE.new && mv -T ... $LIVE`. The Python HUD, rubysat, and (via PYTHONPATH or .pth) vision now see the new tree on next import/restart.
- `systemctl restart rubyhud`.
- Watch 90 s (`WATCH_SECS`): poll `is-active` + delta NRestarts. Any `failed` or >=3 restarts in the window → automatic revert (flip back to prev, restart, clear pending).
- On success: record `hud-state/previous` (for future manual rollback), `last-update.json`, run `self_install` (sync new units + ruby-updated binary itself, daemon-reload, try-restart vision + rubysat non-blocking), then `prune_releases` (keep current + previous + at most one other).
- The health-pending flag is removed only after the watch passes.

`rubyvision.service` deliberately uses `PYTHONPATH=/home/michael/hud/../vision` (release-relative via the live symlink) + `StartLimit=0`. `rubysat.service` relies on the venv editable/.pth re-pinned by `setup-sat.sh` on every apply (see setup-vision.sh history for why the two paths exist and the 2026-06-12 pinning bug that produced "dets=0 despite HEF").

## Rollback paths

- **Manual from HUD/satellite**: ROLLBACK or `ruby_rollback` → `do_rollback` (re-stages from local objects, no net required, same verify + flip + watch).
- **Auto-rollback (OnFailure)**: `ruby-rollback.service` invokes `ruby-updated auto-rollback`.
  - If a fresh `health-pending.json` (<30 min, same boot) exists → flip to its `prev` (the update that just failed the watch).
  - Otherwise: bounded restart attempts (3 per hour) to avoid turning the systemd limiter into a fast crash loop. After the budget, the HUD stays down.
- **Hard manual**: `ruby-updated rollback vX.Y.Z` from the console, or flip the `hud` symlink by hand and `systemctl restart rubyhud`. The previous tag is almost always still in `hud-releases/` and the git objects are local.

`adopt <tag>` is the one-time migration for the pre-git era (moves the old unmanaged tree aside and switches to the release worktree layout).

## Firmware (Qualia satellite)

The ESP32-S3 firmware has **no OTA path** today. Update is manual:

- Edit `secrets.h` (Wi-Fi + fallback IP).
- `arduino-cli compile ...` (exact FQBN in qualia/README.md) or PlatformIO.
- Upload over USB CDC.

Touch orientation flags (`TOUCH_SWAP_XY` etc. in `touch.h`) and the TCA9554 expander address in `panel.cpp` (0x3F vs 0x3E) are the only things that commonly need hardware verification on a new board revision. The 480x480 logical resolution is the firmware truth (see Qualia resolution note below).

After flashing, power-cycle or use the on-device menu to reconnect; the Pi side (rubysat) needs no change unless the STATE schema is extended.

## Vision models and data

- HEF files live in `/home/michael/vision/models/` (michael-owned, survives OTA).
- `setup-vision.sh` creates the dirs but does **not** copy models; copy the desired `yolov8m_h10.hef` (or stub) by hand or via your own rsync.
- Changing the model at runtime is done from the HUD AI VISION page (cycle_model writes `cmd.json`); no restart required.
- The rubyvision service is restarted (non-blocking) by `self_install` after an apply so it picks up any new `rubyvision/` code, but the model dir is stable.

## Setup / install notes (for a fresh Pi or after a hand-edit)

- `deploy/install.sh` (run as root once): installs the handler, units, tmpfiles, sudoers.d scoped NOPASSWD only for vision start/stop/restart, creates state dirs, enables the path unit.
- `deploy/setup.sh` is what `apply` runs on every release. It is safe to run by hand.
- Vision vs sat tension (documented in setup-vision.sh comments): vision pins to the stable `hud-repo/vision` clone (editable or .pth) so that A/B release worktrees don't leave the service importing pruned code. rubysat uses the venv .pth re-pinned at apply time. If the two ever diverge you will see "dets=0" or import failures even though the HEF and code are correct.
- All paths are currently hardcoded for `/home/michael` and the single-vehicle layout. This is by design for Ruby; portability would require a config layer that does not exist today.

## Safety & defensive posture (what the code actually guarantees)

- HUD render loop and all readers (`VisionClient`, future `UpsClient`, CAN DataLayer, touch, etc.) **never raise** into the 15 fps path. Missing files, bad JSON, absent hardware → graceful offline/DEMO/stale display.
- CAN is listen-only.
- UPS ships with `enabled=false, dry_run=true` (pure telemetry; see ups/README.md).
- OTA root handler only ever executes code from a worktree that has been cryptographically verified against a fetched tag.
- Queue drain uses millisecond-timestamp filenames + explicit unlink check to avoid glob/parse races.
- Status files are always written atomically (mkstemp + replace or mv).

If the HUD white-screens at speed, the failure was in a place the "never raise" rule was violated or a lower-level driver (evdev, fb0, lvgl init) hard-crashed.

## Troubleshooting quick ref

- "up to date" but you just tagged: `git push --tags`, then CHECK on the car (or wait for the path unit).
- Stale vision after OTA: the pinning bug (see v3.2.5 / setup-vision.sh history). Confirm `HUD_REPO_VISION` vs the live symlink.
- Updater queue wedged: `deploy/fix-updater-queue.sh` (one-shot).
- No satellite link: check `rubysat-ctl.json` writes, Wi-Fi secrets, mDNS vs fallback IP, and that rubysat is running.
- UPS never acts: it is deliberately disarmed until you edit the config and set both `enabled` and `!dry_run`.
- Disk space: the 500 MiB check is in `do_apply`; free up under `/home/michael` and retry.

## References (code is the source)

- `deploy/ruby-updated` (the ~650-line bash state machine)
- `deploy/ruby-updated.{path,service}`, `ruby-rollback.service`, `rubyhud-health.conf`
- `deploy/setup.sh`, `setup-vision.sh`, `setup-sat.sh`, `install.sh`
- `hud/rubyhud/updates.py` (unprivileged queue client)
- `sat/rubysat/__main__.py` (VERB_MAP + ack)
- `ups/rubyups/{config,monitor,spc}.py`
- `qualia/rubysat-display/` (protocol + FW_VERSION)
- `hud/rubyhud/pages.py` (VisionClient pattern for any future tmpfs client)
- Service files under `deploy/` for PYTHONPATH and restart relationships

When in doubt, read the comments at the top of `ruby-updated` and the VisionClient docstring. They were written by someone who has already been burned.

---

*This file was created to address documentation drift noted in review (missing UPDATING.md despite README references). Keep it current with every release that touches deploy/ or the update UX.*
