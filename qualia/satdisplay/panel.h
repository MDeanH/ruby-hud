// panel.h — Adafruit Qualia ESP32-S3 RGB-666 panel bring-up (Arduino_GFX)
//
// SOURCE OF TRUTH FOR PINS + EXPANDER BRING-UP:
//   Adafruit's standard Arduino_GFX Qualia S3 example (the Adafruit learn-guide
//   "Qualia ESP32-S3 RGB-666" Arduino_GFX sketch). The Qualia routes the
//   panel's 16 RGB data lines + DE/VSYNC/HSYNC/PCLK directly to the ESP32-S3,
//   and gates panel power/reset through an onboard TCA9554 I/O expander, driven
//   in Arduino_GFX via Arduino_XCA9554SWSPI. The GPIO numbers, the SW-SPI
//   expander wiring, and the reset sequence in panel.cpp are copied VERBATIM
//   from that example so the panel powers on and the RGB timings are correct.
//   Do not "tidy" these numbers.
//
// PANEL: TL040HDS20, 720x720, RGB-666 dot-clock, self-initializing — the
//        Arduino_RGB_Display is constructed with NO init operation list.
//
// RGB TIMINGS (verified — task spec):
//   pixel clock 16 MHz
//   hsync_pulse_width 2, hsync_front_porch 46, hsync_back_porch 44
//   vsync_pulse_width 2, vsync_front_porch 16, vsync_back_porch 18
//   hsync/vsync idle low = false, de idle high = false,
//   pclk active neg (active-high = false), pclk idle high = false.

#ifndef RUBYSAT_PANEL_H
#define RUBYSAT_PANEL_H

#include <Arduino_GFX_Library.h>

// Global panel handles (defined in panel.cpp).
extern Arduino_XCA9554SWSPI *xpdr;
extern Arduino_ESP32RGBPanel *rgbpanel;
extern Arduino_RGB_Display   *gfx;

// Bring up the I/O expander, run the Qualia reset sequence, start the RGB bus.
// Leaves `gfx` ready to draw. Returns true on success.
bool panel_begin();

// Backlight control via the TCA9554 expander (menu "backlight test" + HUD
// mirror dimming later).
void panel_backlight(bool on);

#endif  // RUBYSAT_PANEL_H
