# ruby-hud

Dashboard, CAN tooling, and AI vision platform for Ruby — a Raspberry Pi 5 16GB
living in Ruby, a red 2017 Mazda MX-5 Miata GT RF.

- `hud/rubyhud/` — touch HUD (Pillow → framebuffer, tty1, boot default)
- `hud/bin/` — operational scripts (hud-on/off, sims, screenshots, updater CLI)
- `vision/` — Hailo-10H AI vision service (rubyvision)
- `deploy/` — systemd units + install/setup scripts

Releases are annotated tags `vX.Y.Z`; the Pi self-updates to the highest tag
via its on-device updater (see docs/UPDATING.md).
