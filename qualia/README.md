# Qualia rubysat satellite display

A second screen for Ruby. The **Adafruit Qualia ESP32-S3** (4" 720×720 RGB-666,
panel TL040HDS20) renders the gauges **locally in LVGL** and receives live
vehicle state from the Ruby Pi over Wi-Fi. The Pi runs `rubysat` (a small TCP
server, port 7878) that maps the existing rubyhud `Snapshot` + vision status +
SoC temp into a newline-delimited JSON stream. The Qualia's cap-touch sends
commands back. Fully decoupled, network-only — no wires between the Pi and the
display.

Firmware lives in [`rubysat-display/`](rubysat-display/).

## Hardware

One board. **No wiring.** The Qualia integrates the ESP32-S3, the RGB-666 panel
connector, the I/O expander that gates panel power/reset, and the cap-touch
controller (I2C `0x48`) on a single board. Power it over USB-C.

| Fact | Value |
|------|-------|
| Board | Adafruit Qualia ESP32-S3 (8MB octal PSRAM, 16MB flash) |
| Panel | TL040HDS20, 720×720, 4" square, RGB-666 dot-clock, self-initializing |
| Pixel clock | 16 MHz |
| HSYNC | pulse 2, front porch 46, back porch 44 |
| VSYNC | pulse 2, front porch 16, back porch 18 |
| Touch | cap-touch @ I2C `0x48` (shared STEMMA/Wire bus) |

The RGB GPIO pin map and the I/O-expander bring-up/reset sequence are taken
from **Adafruit's standard Arduino_GFX Qualia S3 example** and live in
`rubysat-display/panel.cpp` / `panel.h`. Do not edit those numbers.

## Protocol (must match the Pi `rubysat` server)

- **Transport:** TCP. Ruby = server `0.0.0.0:7878`. Qualia = client (auto-reconnect, exponential backoff to 8s).
- **Encoding:** newline-delimited JSON, UTF-8, ASCII only.
- **Ruby → Qualia** STATE line @ ~15 Hz (heartbeat ≥2 Hz if data stalls):
  ```json
  {"t":<float>,"seq":<int>,"rpm":<int|null>,"mph":<int|null>,"gear":"<str>",
   "coolant":<int|null>,"volts":<float|null>,"throttle":<int|null>,
   "fuel":<int|null>,"bus":"<UP|NO BUS|ERROR>","canfps":<int>,
   "vsrc":"<csi|usb|video|pattern|off>","vdets":<int>,"soc":<float|null>}
  ```
- **Qualia → Ruby** CMD line on touch:
  ```json
  {"cmd":"<page_next|page_prev|tap>","x":<int>,"y":<int>}
  ```

The firmware tolerates partial lines, ignores malformed JSON, and treats any
field as null via sentinels (`--` shown on screen). If no STATE arrives for >2s
the connection dot turns red.

## UI

720×720 dark theme matching the HUD (`bg #07090c`, accent Soul Red `#d0273b`,
text `#f3f7fb`):

- Full-screen **RPM arc** (0–8000, redline ≥6500 turns the arc red).
- Large **MPH** number dead center, `MPH` label under it.
- **Gear** glyph below the speed.
- Bottom row of four mini-bars: **COOL / VOLT / THR / FUEL** (coolant warns >110 °C, volts warn <11.8 V).
- Top-left **CAN chip** (`bus` + `canfps`, border green/amber/red by bus state).
- Top-right **vision chip** (`vsrc` + `vdets`).
- Top-center **connection dot** (green = fresh state, red = stale >2s) + SoC temp.
- Invisible left/right edge tap-zones emit `page_prev` / `page_next`; any other tap emits `tap` with coordinates.

## Set up secrets

```sh
cd rubysat-display
cp secrets.h.example secrets.h
$EDITOR secrets.h        # Wi-Fi creds (home + hotspot) and Ruby fallback IP
```

`secrets.h` is gitignored. The firmware resolves the Pi via mDNS `ruby.local`
first, then falls back to `RUBY_FALLBACK_IP` (default `192.168.2.180`).

## Build & flash — arduino-cli (primary path)

The display's port on Michael's Mac is typically `/dev/cu.usbmodem21201`
(confirm with `arduino-cli board list`). An esptool full-flash backup has
already been taken, so reflashing is reversible.

**One-time core + library install:**

```sh
arduino-cli core update-index
arduino-cli core install esp32:esp32          # Espressif core (3.x)
arduino-cli lib install "GFX Library for Arduino"   # Arduino_GFX (moononournation)
arduino-cli lib install "lvgl"                # install v8.3.x (NOT v9)
arduino-cli lib install "ArduinoJson"
```

**lv_conf.h placement (LVGL quirk):** LVGL needs to find `lv_conf.h`. With
`arduino-cli` the simplest reliable option is to copy this project's
`lv_conf.h` into the Arduino `libraries/` folder, one level **above** the
`lvgl/` directory:

```sh
cp lv_conf.h "$(arduino-cli config get directories.user)/libraries/lv_conf.h"
```

(The sketch also passes `-DLV_CONF_INCLUDE_SIMPLE` so an in-sketch `lv_conf.h`
is honored where the toolchain puts the sketch dir on the include path; copying
to `libraries/` is the belt-and-suspenders move that always works.)

**Compile (exact FQBN):**

```sh
arduino-cli compile \
  --fqbn esp32:esp32:adafruit_qualia_s3_rgb666:PSRAM=opi,FlashSize=16M,PartitionScheme=default_16MB \
  --build-property "compiler.cpp.extra_flags=-DLV_CONF_INCLUDE_SIMPLE" \
  rubysat-display
```

**Fallback FQBN** (if your core predates the Qualia board variant — confirm
with `arduino-cli board listall | grep -i qualia`):

```sh
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=default_16MB,CDCOnBoot=cdc \
  --build-property "compiler.cpp.extra_flags=-DLV_CONF_INCLUDE_SIMPLE -DBOARD_HAS_PSRAM" \
  rubysat-display
```

**Upload:**

```sh
arduino-cli upload \
  --fqbn esp32:esp32:adafruit_qualia_s3_rgb666:PSRAM=opi,FlashSize=16M,PartitionScheme=default_16MB \
  --port /dev/cu.usbmodem21201 \
  rubysat-display

arduino-cli monitor --port /dev/cu.usbmodem21201 --config baudrate=115200
```

If upload fails to start, put the board in bootloader mode (hold **BOOT**, tap
**RESET**, release **BOOT**) and retry. The Qualia exposes a native USB CDC
port — the `cu.usbmodem*` name may change after a reset.

## Build & flash — PlatformIO (alternative)

```sh
cd rubysat-display
pio run                                   # compile
pio run -t upload --upload-port /dev/cu.usbmodem21201
pio device monitor -b 115200
```

See `platformio.ini` for the note on `lv_conf.h` placement under PlatformIO
(move sources + `lv_conf.h` into `src/`, or set `-DLV_CONF_PATH`).

## Files

| File | Purpose |
|------|---------|
| `rubysat-display/rubysat-display.ino` | setup/loop, Wi-Fi, mDNS, TCP client, JSON parse, LVGL plumbing |
| `rubysat-display/panel.h` / `panel.cpp` | Qualia RGB panel + I/O-expander bring-up (Adafruit pin map) |
| `rubysat-display/ui.h` / `ui.cpp` | LVGL gauge layout + setters |
| `rubysat-display/touch.h` | best-effort cap-touch driver @ I2C `0x48` |
| `rubysat-display/secrets.h.example` | template for Wi-Fi + Ruby IP (copy to `secrets.h`) |
| `rubysat-display/lv_conf.h` | LVGL v8 config (RGB565, PSRAM-friendly, the fonts the UI uses) |
| `rubysat-display/platformio.ini` | PlatformIO build config |

## Status / caveats

- **Compile-unverified in CI**: authored against the verified hardware facts;
  the build host had no ESP32 core installed and no board attached. Run the
  `arduino-cli compile` command above to verify.
- **Touch controller @ 0x48 (FocalTech FT5336U) — hardware-verify orientation.**
  For this exact panel (TL040HDS20CT, Adafruit 5794) the cap-touch controller is
  a FocalTech FT5336U (FT6206/FT5x06-class) at I2C `0x48`, per Adafruit's learn
  guide — so the address and the FocalTech register map `touch.h` reads (count at
  reg `0x02`, points from `0x03`, 12-bit high-nibble packing) are the documented
  layout, not a guess. `touch.h` still returns "no touch" cleanly if the chip is
  silent, so the gauges always run. The genuine unknown is panel orientation: the
  `TOUCH_SWAP_XY`/`TOUCH_INVERT_X`/`TOUCH_INVERT_Y` flags default false and are
  untested against the physical mount — flip them if a press lands in the wrong
  place.
- **Panel/expander bring-up** mirrors Adafruit's Qualia S3 Arduino_GFX example
  (TCA9554 expander, reset release, backlight enable). The TCA9554 address is
  set to `0x3F` in `panel.cpp`; if the panel stays dark, try `0x3E`/`0x20`
  (different Qualia revisions) — these are the only values to vary.
