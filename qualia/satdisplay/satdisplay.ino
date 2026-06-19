// satdisplay.ino — Qualia 4" THIN CLIENT for Ruby.
//
// The Pi (rubyhud.satframe) renders the 480x480 Tesla view and streams it here
// as JPEG over USB-serial; this firmware just decodes + blits each frame and
// forwards touches back. So the 4" shares the 7"'s exact look and EVERY UI
// change ships via a normal Pi OTA -- this firmware is flashed ONCE and never
// touched again. (Panel bring-up + cap-touch are reused verbatim from the old
// rubysat-display LVGL firmware via panel.cpp/panel.h/touch.h.)
//
// WIRE PROTOCOL (Pi -> Qualia): frame = 0xA5 0x5A <len:uint32 LE> <jpeg bytes>
//                 (Qualia -> Pi): touch = 0x54 0x43 <x:uint16 LE> <y:uint16 LE>

#include "panel.h"
#include "touch.h"
#include <JPEGDEC.h>

static JPEGDEC jpeg;
static const uint8_t MAGIC0 = 0xA5, MAGIC1 = 0x5A;
static const uint8_t REQ = 0x52;        // 'R' -> Pi: ready, send the next frame
static uint8_t jpgbuf[90000];           // 480x480 JPEG q72 ~15-45 KB; headroom
static uint32_t last_frame_ms = 0;
static uint32_t last_req_ms = 0;

static int jpegDraw(JPEGDRAW *p) {
  gfx->draw16bitRGBBitmap(p->x, p->y, p->pPixels, p->iWidth, p->iHeight);
  return 1;
}

void setup() {
  Serial.begin(921600);                 // native USB CDC; baud is nominal
  Serial.setTxTimeoutMs(10);            // CRITICAL: TinyUSB Serial.write() blocks
                                        // forever if no host is reading -> setup
                                        // would hang here. 10ms drop keeps us live.
  delay(200);                           // let USB CDC enumerate
  panel_begin();                        // leaves gfx ready (black on failure)
  panel_backlight(true);
  touch_begin();
  gfx->fillScreen(0x0000);
  gfx->setTextColor(0x9CD3);            // muted grey
  gfx->setTextSize(2);
  gfx->setCursor(96, 228);
  gfx->print("RUBY  -  waiting for Pi");
  last_req_ms = millis();
  Serial.write(REQ);                    // request the first frame
}

static bool read_exact(uint8_t *buf, size_t n, uint32_t timeout_ms) {
  Serial.setTimeout(timeout_ms);
  return Serial.readBytes(buf, n) == n;
}

void loop() {
  // ---- inbound frame ---------------------------------------------------
  int c = Serial.read();
  if (c == MAGIC0) {
    uint32_t t0 = millis();
    while (Serial.available() < 1 && millis() - t0 < 50) { delay(0); }
    if (Serial.read() == MAGIC1) {
      uint8_t lenb[4];
      if (read_exact(lenb, 4, 300)) {
        uint32_t len = (uint32_t)lenb[0] | ((uint32_t)lenb[1] << 8) |
                       ((uint32_t)lenb[2] << 16) | ((uint32_t)lenb[3] << 24);
        if (len > 0 && len <= sizeof(jpgbuf) &&
            read_exact(jpgbuf, len, 900)) {
          if (jpeg.openRAM(jpgbuf, len, jpegDraw)) {
            jpeg.setPixelType(RGB565_LITTLE_ENDIAN);  // match Arduino_GFX panel
            jpeg.decode(0, 0, 0);
            jpeg.close();
            last_frame_ms = millis();
            Serial.write(REQ);          // ack -> Pi sends the next frame
            last_req_ms = last_frame_ms;
          }
        }
      }
    }
  }

  // ---- outbound touch --------------------------------------------------
  int16_t tx, ty;
  if (touch_get(&tx, &ty)) {
    uint8_t pkt[6] = {0x54, 0x43,
                      (uint8_t)(tx & 0xff), (uint8_t)((tx >> 8) & 0xff),
                      (uint8_t)(ty & 0xff), (uint8_t)((ty >> 8) & 0xff)};
    Serial.write(pkt, sizeof(pkt));
    delay(120);                         // crude debounce
  }

  // ---- re-request if the stream stalled (lost REQ / Pi just started) ----
  if (millis() - last_req_ms > 1000) {
    Serial.write(REQ);
    last_req_ms = millis();
  }
}
