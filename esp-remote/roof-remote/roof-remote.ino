// Ruby roof remote -- Waveshare ESP32-S3-Touch-LCD-1.69 (ST7789V2 + CST816T).
//
// First firmware: draws the roof-remote UI and responds to touch. There is no
// link to the car yet -- tapping OPEN/CLOSE just toggles the on-screen state so
// the display + touchscreen are proven on real hardware. Next step: a BLE client
// that sends open/close/stop to the Pi (which owns the SafetyGate + CAN bridge),
// and shows the real roof status it reports back. Pins are from Waveshare's
// official HARDWARE_REFERENCE.md.
//
// Build/flash (arduino-cli):
//   arduino-cli lib install "GFX Library for Arduino"
//   FQBN=esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,USBMode=hwcdc,CDCOnBoot=cdc
//   arduino-cli compile -b "$FQBN" esp-remote/roof-remote
//   arduino-cli upload  -b "$FQBN" -p /dev/cu.usbmodem3101 esp-remote/roof-remote

#include <Arduino_GFX_Library.h>
#include <Wire.h>

// ---- pins (Waveshare ESP32-S3-Touch-LCD-1.69 HARDWARE_REFERENCE.md) --------
#define LCD_SCK  6
#define LCD_MOSI 7
#define LCD_DC   4
#define LCD_CS   5
#define LCD_RST  8
#define LCD_BL   15
#define TP_SDA   11
#define TP_SCL   10
#define TP_INT   14
#define TP_RST   13
#define TP_ADDR  0x15

#define SCR_W 240
#define SCR_H 280

#define C_BG    RGB565(11, 11, 13)
#define C_RED   RGB565(200, 16, 46)      // Mazda soul red
#define C_WHITE RGB565(255, 255, 255)
#define C_DIM   RGB565(90, 90, 98)
#define C_DARK  RGB565(28, 28, 32)
#define C_TEAL  RGB565(93, 202, 165)

Arduino_DataBus *bus = new Arduino_ESP32SPI(LCD_DC, LCD_CS, LCD_SCK, LCD_MOSI);
Arduino_GFX *gfx = new Arduino_ST7789(bus, LCD_RST, 0 /*rotation*/, true /*IPS*/,
                                      SCR_W, SCR_H, 0, 20, 0, 0);

bool roofOpen = false;
bool wasDown = false;

void drawCentered(const char *t, int y, int size, uint16_t color) {
  int w = (int)strlen(t) * 6 * size;
  gfx->setTextSize(size);
  gfx->setTextColor(color);
  gfx->setCursor((SCR_W - w) / 2, y);
  gfx->print(t);
}

void drawUI() {
  gfx->fillScreen(C_BG);
  gfx->setTextSize(2);
  gfx->setTextColor(C_RED);
  gfx->setCursor(12, 12);
  gfx->print("RUBY");
  gfx->fillCircle(222, 18, 5, C_TEAL);                 // link indicator

  drawCentered(roofOpen ? "OPEN" : "CLOSED", 56, 4, C_WHITE);
  int barw = roofOpen ? 60 : 200;                      // simple roof-state bar
  gfx->fillRoundRect((SCR_W - barw) / 2, 104, barw, 8, 4,
                     roofOpen ? C_TEAL : C_RED);

  gfx->fillRoundRect(10, 132, 220, 58, 12, roofOpen ? C_DARK : C_RED);
  drawCentered("OPEN", 150, 3, roofOpen ? C_DIM : C_WHITE);
  gfx->fillRoundRect(10, 200, 220, 58, 12, roofOpen ? C_RED : C_DARK);
  drawCentered("CLOSE", 218, 3, roofOpen ? C_WHITE : C_DIM);
}

// Minimal CST816T read: finger count + X/Y (12-bit). Returns true on a touch.
bool readTouch(int &x, int &y) {
  Wire.beginTransmission(TP_ADDR);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(TP_ADDR, 5) != 5) return false;
  uint8_t fingers = Wire.read();
  uint8_t xh = Wire.read(), xl = Wire.read();
  uint8_t yh = Wire.read(), yl = Wire.read();
  if (fingers == 0) return false;
  x = ((xh & 0x0F) << 8) | xl;
  y = ((yh & 0x0F) << 8) | yl;
  return true;
}

void setup() {
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);
  gfx->begin();
  pinMode(TP_RST, OUTPUT);
  digitalWrite(TP_RST, LOW);  delay(10);
  digitalWrite(TP_RST, HIGH); delay(50);
  Wire.begin(TP_SDA, TP_SCL);
  drawUI();
}

void loop() {
  int x, y;
  bool down = readTouch(x, y);
  if (down && !wasDown) {                 // act on a fresh press only
    if (y >= 132 && y < 190 && !roofOpen) { roofOpen = true;  drawUI(); }
    else if (y >= 200 && y < 258 && roofOpen) { roofOpen = false; drawUI(); }
    // TODO: replace the on-screen toggle with a BLE open/close/stop to the Pi.
  }
  wasDown = down;
  delay(20);
}
