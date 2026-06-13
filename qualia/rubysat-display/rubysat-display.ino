// rubysat-display.ino — Ruby "rubysat" satellite gauge display
// Board : Adafruit Qualia ESP32-S3 (8MB octal PSRAM)
// Panel : TL040HDS20, 720x720 4" square, RGB-666 dot-clock (self-initializing,
//         no panel command sequence — bring-up is just the I/O-expander reset).
// UI    : LVGL v8 gauges rendered LOCALLY; live state arrives over TCP from the
//         Ruby Pi (rubysat server, port 7878, newline-delimited JSON).
// Touch : cap-touch at I2C 0x48 (best-effort FT5x06-class driver; see touch.h).
//
// Pin map + I/O-expander panel bring-up are taken VERBATIM from the standard
// Adafruit Qualia S3 Arduino_GFX example ("Qualia_S3_RGB666" / the Adafruit
// learn-guide example). See the citation block in panel.h.
//
// Build/flash: see qualia/README.md. Compile-unverified in this environment
// (no ESP32 core / no board attached) — author reviewed by hand.

#include <Arduino_GFX_Library.h>
#include <lvgl.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <WiFiClient.h>
#include <ArduinoJson.h>

#include <Preferences.h>
#include "secrets.h"
#include "panel.h"      // gfx panel + Qualia expander bring-up
#include "touch.h"      // cap-touch best-effort driver
#include "ui.h"         // LVGL gauge UI (build + setters)
#include "menu_ui.h"    // STATUS + MENU tiles (submenus, verbs, toasts)

// menu_ui.cpp hooks
void net_force_reconnect();
void panel_backlight(bool on);
void display_set_rotated(bool rot180);
bool display_is_rotated();
void display_set_mirror(bool on);
bool display_is_mirrored();
void menu_ui_tick();

// Forward declarations (the .ino is one translation unit; declare before use so
// ordering / custom-type signatures never depend on the IDE's auto-prototyper).
static void handle_state_line(const String &line);
static void send_cmd(const char *cmd, int x, int y);

// --------------------------------------------------------------------------- //
// LVGL display plumbing
// --------------------------------------------------------------------------- //
// Partial draw buffer in PSRAM. 720 wide * N lines * 2 bytes (RGB565). A buffer
// of ~1/10th the screen (72 lines) keeps RAM modest while flushing fast. LVGL
// v8 single-buffer partial render is plenty for these gauges.
static const uint32_t SCREEN_W = 480;
static const uint32_t SCREEN_H = 480;
static const uint32_t DRAW_LINES = 48;                  // px/buffer (2 bufs)
static const uint32_t DRAW_BUF_PX = SCREEN_W * DRAW_LINES;

static lv_disp_draw_buf_t draw_buf;
static lv_color_t *buf1 = nullptr;   // double-buffered in INTERNAL SRAM
static lv_color_t *buf2 = nullptr;   // (fast + off the contended PSRAM bus)
static bool g_mirror = false;        // HUD windshield mirror (defined early:
                                     // disp_flush/touch_read use it)

// Flush callback: push the rendered region to the RGB panel. Arduino_GFX's
// draw16bitRGBBitmap takes RGB565 which matches LV_COLOR_DEPTH 16.
static void disp_flush(lv_disp_drv_t *drv, const lv_area_t *area,
                       lv_color_t *color_p) {
  uint32_t w = (area->x2 - area->x1 + 1);
  uint32_t h = (area->y2 - area->y1 + 1);
  if (g_mirror) {
    // Horizontal flip: reverse each row and place the region at the mirrored
    // X (SCREEN_W-1-x2). Line-at-a-time keeps the temp buffer to one row.
    static uint16_t linebuf[SCREEN_W];
    uint16_t *src = (uint16_t *)color_p;
    int dx = (int)SCREEN_W - 1 - (int)area->x2;
    if (w <= SCREEN_W) {
      for (uint32_t row = 0; row < h; row++) {
        uint16_t *r = src + row * w;
        for (uint32_t i = 0; i < w; i++) linebuf[i] = r[w - 1 - i];
        gfx->draw16bitRGBBitmap(dx, area->y1 + row, linebuf, w, 1);
      }
    }
    lv_disp_flush_ready(drv);
    return;
  }
  gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)color_p, w, h);
  lv_disp_flush_ready(drv);
}

// LVGL touch read callback: map the 0x48 cap-touch to 720x720. We edge-detect
// the press here so the loop emits exactly one "tap" CMD per finger-down, not
// one per ~30ms poll while the finger is held.
static bool s_touch_down = false;        // last-poll pressed state
// Touch -> CMD plumbing (declared before touch_read; the auto-prototyper
// hoists functions but not globals).
volatile bool g_touch_event = false;
volatile int16_t g_last_touch_x = 0;
volatile int16_t g_last_touch_y = 0;
static void touch_read(lv_indev_drv_t *drv, lv_indev_data_t *data) {
  int16_t tx, ty;
  if (touch_get(&tx, &ty)) {
    if (g_mirror) tx = (int16_t)SCREEN_W - 1 - tx;   // match the flipped image
    data->state = LV_INDEV_STATE_PRESSED;
    data->point.x = tx;
    data->point.y = ty;
    if (!s_touch_down) {                 // rising edge only
      g_last_touch_x = tx;
      g_last_touch_y = ty;
      g_touch_event = true;
    }
    s_touch_down = true;
  } else {
    data->state = LV_INDEV_STATE_RELEASED;
    s_touch_down = false;
  }
}

// LVGL v8 needs a millisecond tick. Drive it from a hardware timer so timing
// stays correct even when loop() is briefly busy with WiFi/TCP. The ESP32
// timer API differs between Arduino core 2.x and 3.x; both are handled in
// setup() under ESP_ARDUINO_VERSION_MAJOR.
static hw_timer_t *lv_tick_timer = nullptr;
static void ARDUINO_ISR_ATTR on_lv_tick() { lv_tick_inc(1); }

// --------------------------------------------------------------------------- //
// Network state
// --------------------------------------------------------------------------- //
static WiFiClient sock;
static IPAddress ruby_ip;
static bool have_ruby_ip = false;

static uint32_t last_state_ms = 0;        // last STATE line received
static uint32_t last_connect_try = 0;     // backoff clock
static uint32_t connect_backoff = 500;    // ms, grows to a cap
static const uint32_t BACKOFF_MAX = 8000;

static String rxbuf;                      // partial-line accumulator

// --------------------------------------------------------------------------- //
// Tileview (3 swipeable pages) + rotation persistence + RX-rate accounting
// --------------------------------------------------------------------------- //
static lv_obj_t *g_tileview = nullptr;
static lv_obj_t *g_dots[3] = {nullptr, nullptr, nullptr};
static Preferences g_prefs;
static bool g_rot180 = false;
static uint32_t g_rx_lines = 0;        // STATE lines since last rate sample
static float    g_rx_rate = 0.f;
static uint32_t g_rate_t = 0;
static uint32_t g_status_t = 0;

bool display_is_rotated() { return g_rot180; }
void display_set_rotated(bool rot180) {
  g_rot180 = rot180;
  g_prefs.putUChar("rot180", rot180 ? 1 : 0);
  lv_disp_set_rotation(lv_disp_get_default(),
                       rot180 ? LV_DISP_ROT_180 : LV_DISP_ROT_NONE);
}
// HUD mirror: pre-flip the image so it reads correctly reflected off the
// windshield when the panel lies flat on the dash. Horizontal flip (a mirror
// reverses left<->right); applied in disp_flush + touch. Persisted in NVS.
bool display_is_mirrored() { return g_mirror; }
void display_set_mirror(bool on) {
  g_mirror = on;
  g_prefs.putUChar("mirror", on ? 1 : 0);
  lv_obj_invalidate(lv_scr_act());   // force a full redraw in the new mapping
}

void net_force_reconnect();   // defined below near net_pump

static void dots_update(lv_event_t *e) {
  (void)e;
  if (!g_tileview) return;
  lv_obj_t *act = lv_tileview_get_tile_act(g_tileview);
  int idx = 0;
  if (act) idx = lv_obj_get_index(act);
  for (int i = 0; i < 3; i++) {
    if (g_dots[i])
      lv_obj_set_style_bg_color(g_dots[i],
          (i == idx) ? lv_color_hex(0xd0273b) : lv_color_hex(0x2a3340), 0);
  }
}

// --------------------------------------------------------------------------- //
// WiFi
// --------------------------------------------------------------------------- //
// Active Wi-Fi credentials. Loaded from NVS at boot (see setup); the secrets.h
// values are only the first-boot defaults. Editable on-device via the MENU ->
// WI-FI page, which calls wifi_save_creds() below. No hardcoded fallback SSID.
static char g_wifi_ssid[40] = WIFI_SSID;
static char g_wifi_pass[64] = WIFI_PASS;

static void wifi_begin() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                   // latency over power here
  WiFi.begin(g_wifi_ssid, g_wifi_pass);
}

// Non-blocking: returns true once associated. Re-kicks the configured creds
// every ~12s while disconnected so a network that appears late still gets us.
static bool wifi_pump() {
  static uint32_t started = 0;
  if (WiFi.status() == WL_CONNECTED) return true;
  uint32_t now = millis();
  if (started == 0) started = now;
  if ((now - started) > 12000) {
    WiFi.begin(g_wifi_ssid, g_wifi_pass);
    started = now;
  }
  return false;
}

// Persist new Wi-Fi credentials to NVS and reconnect immediately. Called from
// the on-device WI-FI edit page (menu_ui.cpp) via extern. Survives reboot.
void wifi_save_creds(const char *ssid, const char *pass) {
  if (!ssid) return;
  strncpy(g_wifi_ssid, ssid, sizeof(g_wifi_ssid) - 1);
  g_wifi_ssid[sizeof(g_wifi_ssid) - 1] = '\0';
  strncpy(g_wifi_pass, pass ? pass : "", sizeof(g_wifi_pass) - 1);
  g_wifi_pass[sizeof(g_wifi_pass) - 1] = '\0';
  g_prefs.putString("wifi_ssid", g_wifi_ssid);
  g_prefs.putString("wifi_pass", g_wifi_pass);
  // Drop the TCP link + cached Ruby IP and re-associate with the new creds.
  if (sock.connected()) sock.stop();
  have_ruby_ip = false;
  WiFi.disconnect();
  WiFi.begin(g_wifi_ssid, g_wifi_pass);
}

const char *wifi_cfg_ssid() { return g_wifi_ssid; }

// Human-readable association state for the WI-FI page Status row.
const char *wifi_status_str() {
  switch (WiFi.status()) {
    case WL_CONNECTED:     return "Connected";
    case WL_NO_SSID_AVAIL: return "Not found";
    case WL_CONNECT_FAILED: return "Auth failed";
    case WL_CONNECTION_LOST: return "Lost";
    default:               return "Connecting";   // idle/disconnected: retrying
  }
}

// Resolve ruby.local via mDNS; fall back to RUBY_FALLBACK_IP. Cheap to re-call.
static void resolve_ruby() {
  if (have_ruby_ip) return;
  // mDNS must be (re)started after WiFi is up.
  static bool mdns_up = false;
  if (!mdns_up) {
    if (MDNS.begin("rubysat")) mdns_up = true;
  }
  if (mdns_up) {
    IPAddress ip = MDNS.queryHost("ruby");      // "ruby.local"
    if (ip != IPAddress((uint32_t)0)) {
      ruby_ip = ip;
      have_ruby_ip = true;
      return;
    }
  }
  // Fallback static IP from secrets.h.
  if (ruby_ip.fromString(RUBY_FALLBACK_IP)) {
    have_ruby_ip = true;
  }
}

// Non-blocking TCP connect with exponential backoff.
static void net_pump() {
  if (WiFi.status() != WL_CONNECTED) {
    if (sock.connected()) sock.stop();
    return;
  }

  if (!sock.connected()) {
    ui_set_link(false);                         // red chip while down
    uint32_t now = millis();
    if (now - last_connect_try < connect_backoff) return;
    last_connect_try = now;

    resolve_ruby();
    if (!have_ruby_ip) return;

    // connect() with a short timeout so we never block the gauge loop. The
    // ESP32 WiFiClient.connect(timeout_ms) overload is used.
    if (sock.connect(ruby_ip, RUBY_PORT, 1500)) {
      sock.setNoDelay(true);
      connect_backoff = 500;                    // reset backoff on success
      rxbuf = "";
    } else {
      // grow backoff, and let mDNS re-resolve next time in case the IP moved
      have_ruby_ip = false;
      connect_backoff *= 2;
      if (connect_backoff > BACKOFF_MAX) connect_backoff = BACKOFF_MAX;
    }
    return;
  }

  // Connected: drain available bytes, split on '\n', parse each line.
  while (sock.available() > 0) {
    char c = (char)sock.read();
    if (c == '\n') {
      handle_state_line(rxbuf);
      rxbuf = "";
    } else if (c != '\r') {
      if (rxbuf.length() < 1024) rxbuf += c;    // guard against runaway lines
      else rxbuf = "";                          // drop pathological line
    }
  }
}

// Parse a STATE JSON line and push values into the UI.
static void handle_state_line(const String &line) {
  if (line.length() < 2) return;                // ignore blank/keepalive
  StaticJsonDocument<768> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) return;                              // tolerate junk silently

  StateView s;
  s.rpm      = doc["rpm"].isNull()      ? -1   : (int)doc["rpm"];
  s.mph      = doc["mph"].isNull()      ? -1   : (int)doc["mph"];
  // gear/bus/vsrc: copy into fixed buffers (no heap). `doc[k] | "default"`
  // yields a const char* (the default applies when the key is missing/null).
  strlcpy(s.gear, doc["gear"] | "-",      sizeof(s.gear));
  s.coolant  = doc["coolant"].isNull()  ? -1000 : (int)doc["coolant"];
  s.volts    = doc["volts"].isNull()    ? -1.0f : (float)doc["volts"];
  s.throttle = doc["throttle"].isNull() ? -1   : (int)doc["throttle"];
  s.fuel     = doc["fuel"].isNull()     ? -1   : (int)doc["fuel"];
  strlcpy(s.bus,  doc["bus"]  | "NO BUS", sizeof(s.bus));
  s.canfps   = doc["canfps"] | 0;
  strlcpy(s.vsrc, doc["vsrc"] | "off",    sizeof(s.vsrc));
  s.vdets    = doc["vdets"] | 0;
  s.soc      = doc["soc"].isNull()      ? -1000.0f : (float)doc["soc"];

  ui_update(s);
  last_state_ms = millis();
  ui_set_link(true);
  g_rx_lines++;

  // Satellite control from the 7" Ruby HUD: transient {"ctl":{"seq":N,
  // "cmd":"..."}} rides STATE lines for a few seconds; seq-deduped here.
  if (doc["ctl"].is<JsonObject>()) {
    static int last_ctl_seq = 0;
    int cseq = doc["ctl"]["seq"] | 0;
    const char *ccmd = doc["ctl"]["cmd"] | "";
    if (cseq != 0 && cseq != last_ctl_seq && ccmd[0]) {
      last_ctl_seq = cseq;
      if      (!strcmp(ccmd, "mirror_on"))     display_set_mirror(true);
      else if (!strcmp(ccmd, "mirror_off"))    display_set_mirror(false);
      else if (!strcmp(ccmd, "mirror_toggle")) display_set_mirror(!display_is_mirrored());
      else if (!strcmp(ccmd, "rot_toggle"))    display_set_rotated(!display_is_rotated());
      else if (!strcmp(ccmd, "sat_page0") && g_tileview) lv_obj_set_tile_id(g_tileview, 0, 0, LV_ANIM_ON);
      else if (!strcmp(ccmd, "sat_page1") && g_tileview) lv_obj_set_tile_id(g_tileview, 1, 0, LV_ANIM_ON);
      else if (!strcmp(ccmd, "sat_page2") && g_tileview) lv_obj_set_tile_id(g_tileview, 2, 0, LV_ANIM_ON);
      else if (!strcmp(ccmd, "backlight_on"))  panel_backlight(true);
      else if (!strcmp(ccmd, "backlight_off")) panel_backlight(false);
    }
  }

  // Optional verb ack riding the STATE stream -> toast + ABOUT row.
  const char *ack = doc["ack"] | "";
  if (ack[0]) menu_ui_set_ack(ack);

  // ~1 Hz: refresh STATUS tile values + RX rate.
  uint32_t now = millis();
  if (now - g_status_t > 1000) {
    g_status_t = now;
    if (now - g_rate_t >= 1000) {
      g_rx_rate = g_rx_lines * 1000.0f / (float)(now - g_rate_t);
      g_rx_lines = 0; g_rate_t = now;
    }
    menu_ui_set_state(s.bus, s.canfps, s.vsrc, s.vdets, s.soc,
                      (millis() - last_state_ms) < 2000);
    menu_ui_set_net(WiFi.SSID().c_str(),
                    WiFi.localIP().toString().c_str(),
                    have_ruby_ip ? ruby_ip.toString().c_str() : "--",
                    WiFi.RSSI(), g_rx_rate);
  }
}

// Emit a CMD line on touch. Non-blocking write; drops if socket is busy/down.
void net_force_reconnect() {
  if (sock.connected()) sock.stop();
  have_ruby_ip = false;          // re-resolve (IP may have moved)
  connect_backoff = 500;
  last_connect_try = 0;
}

static void send_cmd(const char *cmd, int x, int y) {
  if (!sock.connected()) return;
  StaticJsonDocument<96> doc;
  doc["cmd"] = cmd;
  doc["x"] = x;
  doc["y"] = y;
  char out[96];
  size_t n = serializeJson(doc, out, sizeof(out));
  if (n > 0 && n < sizeof(out) - 1) {
    out[n] = '\n';
    sock.write((const uint8_t *)out, n + 1);
  }
}

// --------------------------------------------------------------------------- //
// setup()
// --------------------------------------------------------------------------- //
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[rubysat] boot");

  // 1) Panel bring-up: Qualia I/O-expander reset sequence, then start the
  //    RGB bus. panel_begin() also leaves `gfx` ready to draw.
  if (!panel_begin()) {
    Serial.println("[rubysat] PANEL INIT FAILED");
    // Keep going — a black screen is still better than a hang, and serial logs
    // will show the failure.
  }
  gfx->fillScreen(0x0000);

  // 2) LVGL core init.
  lv_init();

  // Allocate the draw buffer in PSRAM (ps_malloc -> heap_caps_malloc OPI PSRAM).
  // Double buffer in INTERNAL SRAM (fast, and off the PSRAM bus the RGB panel
  // is scanning out -> higher LVGL fps AND less scanout contention). Fall back
  // to a single PSRAM buffer if internal allocation fails.
  size_t bytes = DRAW_BUF_PX * sizeof(lv_color_t);
  buf1 = (lv_color_t *)heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  buf2 = (lv_color_t *)heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (buf1 && buf2) {
    lv_disp_draw_buf_init(&draw_buf, buf1, buf2, DRAW_BUF_PX);
    Serial.println("[rubysat] draw buf: 2x internal SRAM");
  } else {
    if (buf1) { heap_caps_free(buf1); }
    if (buf2) { heap_caps_free(buf2); buf2 = nullptr; }
    buf1 = (lv_color_t *)ps_malloc(bytes);
    if (!buf1) buf1 = (lv_color_t *)malloc(bytes);
    lv_disp_draw_buf_init(&draw_buf, buf1, nullptr, DRAW_BUF_PX);
    Serial.println("[rubysat] draw buf: 1x fallback");
  }

  static lv_disp_drv_t disp_drv;
  lv_disp_drv_init(&disp_drv);
  disp_drv.hor_res = SCREEN_W;
  disp_drv.ver_res = SCREEN_H;
  disp_drv.flush_cb = disp_flush;
  disp_drv.draw_buf = &draw_buf;
  lv_disp_drv_register(&disp_drv);

  // Touch input device.
  touch_begin();
  static lv_indev_drv_t indev_drv;
  lv_indev_drv_init(&indev_drv);
  indev_drv.type = LV_INDEV_TYPE_POINTER;
  indev_drv.read_cb = touch_read;
  lv_indev_drv_register(&indev_drv);

  // 1ms LVGL tick from a hardware timer. The Arduino-ESP32 timer API changed
  // in core 3.x: timerBegin(freq) + timerAlarm(...), vs core 2.x's
  // timerBegin(num, divider, countUp) + timerAlarmWrite/Enable.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && (ESP_ARDUINO_VERSION_MAJOR >= 3)
  lv_tick_timer = timerBegin(1000000);                 // 1MHz tick
  timerAttachInterrupt(lv_tick_timer, &on_lv_tick);
  timerAlarm(lv_tick_timer, 1000, true, 0);            // 1000us = 1ms, repeat
#else
  lv_tick_timer = timerBegin(0, 80, true);             // 80MHz/80 = 1MHz
  timerAttachInterrupt(lv_tick_timer, &on_lv_tick, true);
  timerAlarmWrite(lv_tick_timer, 1000, true);          // 1000us = 1ms
  timerAlarmEnable(lv_tick_timer);
#endif

  // 2b) Restore persisted rotation BEFORE building UI.
  g_prefs.begin("rubysat", false);
  g_rot180 = g_prefs.getUChar("rot180", 0) ? true : false;
  g_mirror = g_prefs.getUChar("mirror", 0) ? true : false;
  // Wi-Fi creds: NVS overrides the secrets.h first-boot defaults. Editable on
  // the device (MENU -> WI-FI); persisted by wifi_save_creds().
  {
    String ns = g_prefs.getString("wifi_ssid", WIFI_SSID);
    String np = g_prefs.getString("wifi_pass", WIFI_PASS);
    strncpy(g_wifi_ssid, ns.c_str(), sizeof(g_wifi_ssid) - 1);
    g_wifi_ssid[sizeof(g_wifi_ssid) - 1] = '\0';
    strncpy(g_wifi_pass, np.c_str(), sizeof(g_wifi_pass) - 1);
    g_wifi_pass[sizeof(g_wifi_pass) - 1] = '\0';
  }

  // 3) Tileview: 3 horizontally-swipeable tiles (GAUGES / STATUS / MENU).
  g_tileview = lv_tileview_create(lv_scr_act());
  lv_obj_set_style_bg_color(g_tileview, lv_color_hex(0x07090c), 0);
  lv_obj_set_scrollbar_mode(g_tileview, LV_SCROLLBAR_MODE_OFF);
  lv_obj_t *t_gauges = lv_tileview_add_tile(g_tileview, 0, 0, LV_DIR_RIGHT);
  lv_obj_t *t_status = lv_tileview_add_tile(g_tileview, 1, 0, LV_DIR_LEFT | LV_DIR_RIGHT);
  lv_obj_t *t_menu   = lv_tileview_add_tile(g_tileview, 2, 0, LV_DIR_LEFT);
  lv_obj_add_event_cb(g_tileview, dots_update, LV_EVENT_VALUE_CHANGED, nullptr);

  // GAUGES tile: existing cluster, reparented into the tile.
  ui_init(t_gauges);
  ui_set_link(false);

  // STATUS + MENU tiles.
  menu_ui_init(t_status, t_menu);

  // Floating page dots on the top layer (visible across all tiles).
  for (int i = 0; i < 3; i++) {
    g_dots[i] = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(g_dots[i]);
    lv_obj_set_size(g_dots[i], 12, 12);
    lv_obj_set_style_radius(g_dots[i], LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(g_dots[i], LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(g_dots[i],
        (i == 0) ? lv_color_hex(0xd0273b) : lv_color_hex(0x2a3340), 0);
    lv_obj_align(g_dots[i], LV_ALIGN_BOTTOM_MID, (i - 1) * 22, -8);
  }

  // Apply persisted rotation now that a display exists.
  if (g_rot180)
    lv_disp_set_rotation(lv_disp_get_default(), LV_DISP_ROT_180);

  // 4) WiFi.
  wifi_begin();

  Serial.println("[rubysat] setup done");
}

// --------------------------------------------------------------------------- //
// loop()
// --------------------------------------------------------------------------- //
void loop() {
  // a) WiFi association (non-blocking).
  wifi_pump();

  // b) TCP connect/receive (non-blocking).
  net_pump();

  // c) Staleness: if no STATE for >2s, flag link red even if socket is up.
  if (millis() - last_state_ms > 2000) {
    ui_set_link(false);
  }

  // e) LVGL service FIRST: this runs the indev read (touch_read) and dispatches
  //    edge-zone LV_EVENT_CLICKED -> g_pending_cmd, all within this call.
  lv_timer_handler();

  // f) Touch -> CMD, consuming the gesture LVGL just processed. A page button
  //    (edge zone) takes priority; if the same touch also set g_touch_event we
  //    clear it so we don't also emit a stray "tap". A raw tap with no widget
  //    emits "tap" + coordinates.
  if (g_pending_cmd[0] != '\0') {
    send_cmd(g_pending_cmd, g_pending_cmd_x, g_pending_cmd_y);
    g_pending_cmd[0] = '\0';
    g_touch_event = false;            // same physical touch — don't double-send
  } else if (g_touch_event) {
    g_touch_event = false;
    send_cmd("tap", g_last_touch_x, g_last_touch_y);
  }

  // g) expire menu toasts.
  menu_ui_tick();

  // Small yield; LVGL refresh is timer-driven so this just paces the loop.
  delay(2);
}
