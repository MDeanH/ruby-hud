# Ruby CarPlay bridge

Apple CarPlay on Ruby's Pi 5, via the open-source **node-carplay** stack + a
**CarlinKit CPC200-CCPA/CCPM** USB dongle (the dongle does the Apple MFi
handshake — there is no software-only CarPlay on a Pi).

## Why not react-carplay (Electron)?

Ruby runs console + framebuffer with **no X/Wayland**. The Electron-based
`react-carplay` needs a display server, which would conflict with the
Pillow→`/dev/fb0` HUD. Instead we use the `node-carplay` library headless and
render its H.264 video straight to **DRM/KMS** (Pi 5 has hardware H.264
*decode*), with rubyhud paused while CarPlay owns the screen.

## Architecture

```
CarlinKit dongle ──USB──> node-carplay (Node) ──H.264──> DRM/KMS player ──> panel
   (MFi handshake)          + audio (PCM)  + touch          (gstreamer/ffmpeg)
                                  ▲                              while rubyhud
                          touch events from /dev/input          is paused
```

- **Detect:** dongle = USB `0x1314:0x1520` / `0x1314:0x1521`
  (`DongleDriver.knownDevices`, node-carplay 4.1.0).
- **Display handoff:** stop/pause rubyhud (frees `/dev/fb0`), CarPlay player
  takes DRM, restore rubyhud on exit. (Riskiest part — fbcon vs DRM master;
  validate on hardware.)
- **Audio:** node-carplay PCM → ALSA. **Touch:** `/dev/input` → node-carplay.

## Files

- `package.json` — deps (`node-carplay`). Runtime lives at `~/carplay` on the
  Pi; `node_modules` is gitignored (run `npm install` there).
- `probe.js` — detect the dongle (matched against `DongleDriver.knownDevices`).
  `node probe.js` → exit 0 if a dongle is present, 2 if not.

## Status (2026-06-12)

- ✅ Node 20.19.2 + npm + `node-carplay@4.1.0` installed at `~/carplay` (Pi).
  The `usb` native binding has prebuilt arm64 — no build tools needed.
- ✅ `probe.js` works (reports no dongle today).
- ⏳ **Blocked on hardware:** no CarlinKit dongle plugged in. Connector
  (stream→player), display handoff, audio/touch routing all need the dongle to
  build/verify. Recommended dongle: **CarlinKit CPC200-CCPA** (wired, ~$50-100).
