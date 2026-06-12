// panel.cpp — Qualia ESP32-S3 RGB-666 panel construction + bring-up.
//
// SOURCE: Adafruit's Qualia S3 RGB666 factory test / Arduino_GFX example
//   (Adafruit_Learning_System_Guides, Qualia_ESP32S3_RGB666_FactoryTest).
//   The constructor argument order and the I/O-expander reset wiring are copied
//   from there. The pin NAMES (TFT_*, PCA_TFT_*) are defined by the
//   adafruit_qualia_s3_rgb666 board variant's pins_arduino.h and resolve to the
//   correct GPIOs when compiled with that FQBN. Numeric fallbacks (matching the
//   variant exactly) are provided so the sketch still builds under the generic
//   esp32:esp32:esp32s3 FQBN.
//
// PANEL: TL040HDS20, 720x720, RGB-666, self-initializing -> Arduino_RGB_Display
//        is constructed with NO init operation list.

#include "panel.h"
#include <Wire.h>   // Wire.begin(8,18) in panel_begin() pins I2C for both FQBNs

// --------------------------------------------------------------------------- //
// Pin fallbacks (only used if the board variant didn't define them). Values are
// the verified adafruit_qualia_s3_rgb666 pins_arduino.h numbers.
// --------------------------------------------------------------------------- //
#ifndef TFT_DE
  #define TFT_DE 2
  #define TFT_VSYNC 42
  #define TFT_HSYNC 41
  #define TFT_PCLK 1
  #define TFT_R1 11
  #define TFT_R2 10
  #define TFT_R3 9
  #define TFT_R4 46
  #define TFT_R5 3
  #define TFT_G0 48
  #define TFT_G1 47
  #define TFT_G2 21
  #define TFT_G3 14
  #define TFT_G4 13
  #define TFT_G5 12
  #define TFT_B1 40
  #define TFT_B2 39
  #define TFT_B3 38
  #define TFT_B4 0
  #define TFT_B5 45
#endif

#ifndef PCA_TFT_RESET
  #define PCA_TFT_RESET 2
  #define PCA_TFT_CS 1
  #define PCA_TFT_SCK 0
  #define PCA_TFT_MOSI 7
  #define PCA_TFT_BACKLIGHT 4
#endif

#ifndef SDA
  #define SDA 8
#endif
#ifndef SCL
  #define SCL 18
#endif

Arduino_XCA9554SWSPI *xpdr      = nullptr;
Arduino_ESP32RGBPanel *rgbpanel = nullptr;
Arduino_RGB_Display   *gfx       = nullptr;

bool panel_begin() {
  // 0) Bring up I2C on the Qualia's real pins (SDA=8, SCL=18) BEFORE anything
  //    touches the bus. Arduino_XCA9554SWSPI::begin() (called inside gfx->begin()
  //    below) does _wire->begin() with NO args, which under the fallback FQBN
  //    esp32:esp32:esp32s3 defaults to the generic esp32s3 variant's SDA=8/SCL=9
  //    -- the TCA9554 @ 0x3F would be probed on the wrong SCL, never ACK, and the
  //    expander-driven panel RESET would never assert (dark screen). Doing an
  //    explicit Wire.begin(8,18) first pins the correct pins for both FQBNs
  //    (harmless on the primary adafruit_qualia_s3_rgb666 FQBN, which already
  //    defaults to 8/18). touch.h's later Wire.begin(8,18) is then a no-op.
  //    NOTE: use LITERAL 8,18 -- NOT the SDA/SCL macros. The generic esp32s3
  //    variant DEFINES SDA=8/SCL=9 itself, so our #ifndef SCL fallback (18)
  //    would NOT apply under the fallback FQBN and SCL would expand to 9. The
  //    literals are the verified Qualia pins regardless of which variant is
  //    active.
  Wire.begin(8, 18);  // SDA=8, SCL=18 (verified Qualia I2C pins)

  // 1) I/O expander on the shared I2C bus (TCA9554 @ 0x3F on the Qualia). The
  //    expander bit-bangs the panel's reset/CS/SCK/MOSI and the backlight.
  xpdr = new Arduino_XCA9554SWSPI(
      PCA_TFT_RESET, PCA_TFT_CS, PCA_TFT_SCK, PCA_TFT_MOSI,
      &Wire, 0x3F);

  // 2) RGB panel: DE, VSYNC, HSYNC, PCLK, then R1..R5, G0..G5, B1..B5 (the
  //    RGB666 panel's high bits wired to these GPIOs), then the verified sync
  //    timings: hsync(pol,fp,pw,bp) = 1,46,2,44 ; vsync = 1,16,2,18.
  //    (Adafruit's 480x480 example uses hsync fp=50; this 720x720 TL040HDS20
  //    uses fp=46 per the task's verified timings.)
  rgbpanel = new Arduino_ESP32RGBPanel(
      TFT_DE, TFT_VSYNC, TFT_HSYNC, TFT_PCLK,
      TFT_R1, TFT_R2, TFT_R3, TFT_R4, TFT_R5,
      TFT_G0, TFT_G1, TFT_G2, TFT_G3, TFT_G4, TFT_G5,
      TFT_B1, TFT_B2, TFT_B3, TFT_B4, TFT_B5,
      1 /* hsync_polarity */, 46 /* hsync_front_porch */,
      2 /* hsync_pulse_width */, 44 /* hsync_back_porch */,
      1 /* vsync_polarity */, 16 /* vsync_front_porch */,
      2 /* vsync_pulse_width */, 18 /* vsync_back_porch */,
      1 /* pclk_active_neg (pclk active-high = false) */,
      16000000 /* prefer_speed = 16 MHz */);

  // 3) RGB display: 720x720, rotation 0, auto_flush, the expander passed as the
  //    DataBus (panel-control) handle, rst = GFX_NOT_DEFINED (reset is gated by
  //    the expander), and NO init operations (self-initializing panel).
  //    Signature: (w,h,rgbpanel,r,auto_flush,bus,rst,init_ops,init_ops_len,...)
  gfx = new Arduino_RGB_Display(
      720, 720, rgbpanel, 0 /* rotation */, true /* auto_flush */,
      xpdr /* bus = I/O expander */, GFX_NOT_DEFINED /* rst via expander */,
      nullptr /* init_operations = none */, 0 /* init_operations_len */);

  if (!gfx->begin()) {
    return false;
  }

  // 4) Enable the panel backlight via the expander (required for a visible
  //    image even though the timing controller self-inits).
  xpdr->pinMode(PCA_TFT_BACKLIGHT, OUTPUT);
  xpdr->digitalWrite(PCA_TFT_BACKLIGHT, HIGH);

  gfx->fillScreen(0x0000);
  return true;
}
