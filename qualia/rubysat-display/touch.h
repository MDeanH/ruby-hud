// touch.h — cap-touch driver for the Qualia panel @ I2C 0x48.
//
// CONTROLLER: for THIS exact panel (TL040HDS20CT, Adafruit 5794) the cap-touch
// controller is a FocalTech FT5336U (FT6206/FT5x06-class) at I2C 0x48, per
// Adafruit's learn guide. The register map this driver reads is the correct
// FocalTech layout: touch count at reg 0x02, point bytes from reg 0x03, 12-bit
// X/Y high-nibble packing, read starting at reg 0x00. So 0x48 is the documented
// address here -- NOT a blind guess -- and the read pattern matches the part.
//
// This driver:
//   * probes 0x48 and reads the FocalTech FT5x06-style register block,
//   * maps raw coordinates to the 720x720 panel,
//   * if the chip is silent or the layout doesn't match, returns "no touch"
//     so the UI still runs. It NEVER blocks.
//
// HARDWARE-VERIFY (genuine unknowns): the axis swap/invert flags below
// (TOUCH_SWAP_XY / TOUCH_INVERT_X / TOUCH_INVERT_Y) default false and are
// untested against the physical panel orientation -- flip them if a press lands
// in the wrong place. The address and register map themselves are documented.

#ifndef RUBYSAT_TOUCH_H
#define RUBYSAT_TOUCH_H

#include <Arduino.h>
#include <Wire.h>

// Touch I2C address: 0x48 (FocalTech FT5336U on the TL040HDS20CT, documented).
static const uint8_t TOUCH_ADDR = 0x48;

// Panel geometry for coordinate mapping.
static const int16_t TOUCH_PANEL_W = 720;
static const int16_t TOUCH_PANEL_H = 720;

// Set true if testing shows X or Y must be inverted / swapped for this panel
// orientation. Defaults assume native portrait-square, origin top-left.
static const bool TOUCH_SWAP_XY = false;
static const bool TOUCH_INVERT_X = false;
static const bool TOUCH_INVERT_Y = false;

static bool s_touch_present = false;

// Probe the controller once. Best-effort: marks presence if the device ACKs.
inline void touch_begin() {
  // Wire is already begun by the panel expander on SDA=8/SCL=18; ensure it.
  Wire.begin(8, 18);
  Wire.setClock(400000);
  Wire.beginTransmission(TOUCH_ADDR);
  s_touch_present = (Wire.endTransmission() == 0);
}

// Read one touch point. Returns true and fills *x,*y (720x720 space) on a
// valid press; false otherwise. Non-blocking, tolerant of a missing/foreign
// controller.
inline bool touch_get(int16_t *x, int16_t *y) {
  if (!s_touch_present) {
    // Re-probe occasionally in case the bus settled after boot.
    static uint32_t next_probe = 0;
    if (millis() >= next_probe) {
      next_probe = millis() + 1000;
      Wire.beginTransmission(TOUCH_ADDR);
      s_touch_present = (Wire.endTransmission() == 0);
    }
    if (!s_touch_present) return false;
  }

  // FT5x06-style read: set register pointer to 0x00, read 7 bytes.
  //   [0] mode, [1] gesture, [2] touch count (low nibble),
  //   [3] XH (bits3-0 = X high nibble, bits7-6 = event flag),
  //   [4] XL, [5] YH (bits3-0 = Y high nibble), [6] YL
  Wire.beginTransmission(TOUCH_ADDR);
  Wire.write((uint8_t)0x00);
  if (Wire.endTransmission(false) != 0) {        // repeated-start
    s_touch_present = false;
    return false;
  }
  const uint8_t want = 7;
  uint8_t got = Wire.requestFrom((int)TOUCH_ADDR, (int)want);
  if (got < want) return false;

  uint8_t b[7];
  for (uint8_t i = 0; i < want; i++) b[i] = Wire.read();

  uint8_t touches = b[2] & 0x0F;
  if (touches == 0 || touches > 5) return false;  // no/invalid touch

  uint16_t rawx = ((uint16_t)(b[3] & 0x0F) << 8) | b[4];
  uint16_t rawy = ((uint16_t)(b[5] & 0x0F) << 8) | b[6];

  // FT-class panels report in their native resolution (== panel res here).
  int16_t mx = rawx;
  int16_t my = rawy;

  if (TOUCH_SWAP_XY) { int16_t t = mx; mx = my; my = t; }
  if (TOUCH_INVERT_X) mx = TOUCH_PANEL_W - 1 - mx;
  if (TOUCH_INVERT_Y) my = TOUCH_PANEL_H - 1 - my;

  // Clamp into panel bounds.
  if (mx < 0) mx = 0; else if (mx >= TOUCH_PANEL_W) mx = TOUCH_PANEL_W - 1;
  if (my < 0) my = 0; else if (my >= TOUCH_PANEL_H) my = TOUCH_PANEL_H - 1;

  *x = mx;
  *y = my;
  return true;
}

#endif  // RUBYSAT_TOUCH_H
