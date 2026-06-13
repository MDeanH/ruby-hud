// rubysat-display.ino — Ruby "rubysat" satellite gauge display
// Board : Adafruit Qualia ESP32-S3 (8MB octal PSRAM)
// Panel : TL040HDS20, 480x480 4" square, RGB-666 dot-clock.
// Link  : USB CDC (plug into Pi) or Wi-Fi TCP (rubysat :7878), selectable on
//         the 4" CONNECT menu (AUTO tries USB first, then Wi-Fi).
// UI    : LVGL v8 gauges rendered LOCALLY; live state over newline JSON.
// Touch : cap-touch at I2C 0x48 (best-effort FT5x06-class driver; see touch.h).

#include <Arduino_GFX_Library.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>

#include "secrets.h"
#include "panel.h"
#include "touch.h"
#include "ui.h"
#include "menu_ui.h"
#include "netlink.h"

void panel_backlight(bool on);
void display_set_rotated(bool rot180);
bool display_is_rotated();
void display_set_mirror(bool on);
bool display_is_mirrored();
void menu_ui_tick();

// --------------------------------------------------------------------------- //
// LVGL display plumbing
// --------------------------------------------------------------------------- //
static const uint32_t SCREEN_W = 480;
static const uint32_t SCREEN_H = 480;
static const uint32_t DRAW_LINES = 48;
static const uint32_t DRAW_BUF_PX = SCREEN_W * DRAW_LINES;

static lv_disp_draw_buf_t draw_buf;
static lv_color_t *buf1 = nullptr;
static lv_color_t *buf2 = nullptr;
static bool g_mirror = false;

static void disp_flush(lv_disp_drv_t *drv, const lv_area_t *area,
                       lv_color_t *color_p) {
  uint32_t w = (area->x2 - area->x1 + 1);
  uint32_t h = (area->y2 - area->y1 + 1);
  if (g_mirror) {
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

static bool s_touch_down = false;
volatile bool g_touch_event = false;
volatile int16_t g_last_touch_x = 0;
volatile int16_t g_last_touch_y = 0;

static void touch_read(lv_indev_drv_t *drv, lv_indev_data_t *data) {
  int16_t tx, ty;
  if (touch_get(&tx, &ty)) {
    if (g_mirror) tx = (int16_t)SCREEN_W - 1 - tx;
    data->state = LV_INDEV_STATE_PRESSED;
    data->point.x = tx;
    data->point.y = ty;
    if (!s_touch_down) {
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

static hw_timer_t *lv_tick_timer = nullptr;
static void ARDUINO_ISR_ATTR on_lv_tick() { lv_tick_inc(1); }

// --------------------------------------------------------------------------- //
// Tileview + rotation persistence
// --------------------------------------------------------------------------- //
static lv_obj_t *g_tileview = nullptr;
static lv_obj_t *g_dots[3] = {nullptr, nullptr, nullptr};
static Preferences g_prefs;
static bool g_rot180 = false;
static uint32_t g_rx_lines = 0;
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

bool display_is_mirrored() { return g_mirror; }
void display_set_mirror(bool on) {
  g_mirror = on;
  g_prefs.putUChar("mirror", on ? 1 : 0);
  lv_obj_invalidate(lv_scr_act());
}

void net_force_reconnect() {
  netlink_force_reconnect();
}

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

// Called by netlink when a STATE JSON line arrives (USB or Wi-Fi).
void netlink_on_state_line(const String &line) {
  if (line.length() < 2) return;
  StaticJsonDocument<768> doc;
  if (deserializeJson(doc, line)) return;

  StateView s;
  s.rpm      = doc["rpm"].isNull()      ? -1   : (int)doc["rpm"];
  s.mph      = doc["mph"].isNull()      ? -1   : (int)doc["mph"];
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
  ui_set_link(true);
  g_rx_lines++;

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

  const char *ack = doc["ack"] | "";
  if (ack[0]) menu_ui_set_ack(ack);

  uint32_t now = millis();
  if (now - g_status_t > 1000) {
    g_status_t = now;
    if (now - g_rate_t >= 1000) {
      g_rx_rate = g_rx_lines * 1000.0f / (float)(now - g_rate_t);
      g_rx_lines = 0;
      g_rate_t = now;
    }
    menu_ui_set_state(s.bus, s.canfps, s.vsrc, s.vdets, s.soc,
                      netlink_link_up());
    const char *ssid = netlink_wifi_up() ? WiFi.SSID().c_str() : "--";
    const char *myip = netlink_wifi_up() ? WiFi.localIP().toString().c_str() : "--";
    menu_ui_set_net(ssid, myip, netlink_ruby_ip(),
                    netlink_rssi(), g_rx_rate);
  }
}

// --------------------------------------------------------------------------- //
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[rubysat] boot");

  if (!panel_begin()) {
    Serial.println("[rubysat] PANEL INIT FAILED");
  }
  gfx->fillScreen(0x0000);

  lv_init();

  size_t bytes = DRAW_BUF_PX * sizeof(lv_color_t);
  buf1 = (lv_color_t *)heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  buf2 = (lv_color_t *)heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (buf1 && buf2) {
    lv_disp_draw_buf_init(&draw_buf, buf1, buf2, DRAW_BUF_PX);
  } else {
    if (buf1) heap_caps_free(buf1);
    if (buf2) { heap_caps_free(buf2); buf2 = nullptr; }
    buf1 = (lv_color_t *)ps_malloc(bytes);
    if (!buf1) buf1 = (lv_color_t *)malloc(bytes);
    lv_disp_draw_buf_init(&draw_buf, buf1, nullptr, DRAW_BUF_PX);
  }

  static lv_disp_drv_t disp_drv;
  lv_disp_drv_init(&disp_drv);
  disp_drv.hor_res = SCREEN_W;
  disp_drv.ver_res = SCREEN_H;
  disp_drv.flush_cb = disp_flush;
  disp_drv.draw_buf = &draw_buf;
  lv_disp_drv_register(&disp_drv);

  touch_begin();
  static lv_indev_drv_t indev_drv;
  lv_indev_drv_init(&indev_drv);
  indev_drv.type = LV_INDEV_TYPE_POINTER;
  indev_drv.read_cb = touch_read;
  lv_indev_drv_register(&indev_drv);

#if defined(ESP_ARDUINO_VERSION_MAJOR) && (ESP_ARDUINO_VERSION_MAJOR >= 3)
  lv_tick_timer = timerBegin(1000000);
  timerAttachInterrupt(lv_tick_timer, &on_lv_tick);
  timerAlarm(lv_tick_timer, 1000, true, 0);
#else
  lv_tick_timer = timerBegin(0, 80, true);
  timerAttachInterrupt(lv_tick_timer, &on_lv_tick, true);
  timerAlarmWrite(lv_tick_timer, 1000, true);
  timerAlarmEnable(lv_tick_timer);
#endif

  g_prefs.begin("rubysat", false);
  g_rot180 = g_prefs.getUChar("rot180", 0) ? true : false;
  g_mirror = g_prefs.getUChar("mirror", 0) ? true : false;

  g_tileview = lv_tileview_create(lv_scr_act());
  lv_obj_set_style_bg_color(g_tileview, lv_color_hex(0x07090c), 0);
  lv_obj_set_scrollbar_mode(g_tileview, LV_SCROLLBAR_MODE_OFF);
  lv_obj_t *t_gauges = lv_tileview_add_tile(g_tileview, 0, 0, LV_DIR_RIGHT);
  lv_obj_t *t_status = lv_tileview_add_tile(g_tileview, 1, 0, LV_DIR_LEFT | LV_DIR_RIGHT);
  lv_obj_t *t_menu   = lv_tileview_add_tile(g_tileview, 2, 0, LV_DIR_LEFT);
  lv_obj_add_event_cb(g_tileview, dots_update, LV_EVENT_VALUE_CHANGED, nullptr);

  ui_init(t_gauges);
  ui_set_link(false);
  menu_ui_init(t_status, t_menu);

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

  if (g_rot180)
    lv_disp_set_rotation(lv_disp_get_default(), LV_DISP_ROT_180);

  netlink_begin();
  Serial.println("[rubysat] setup done");
}

void loop() {
  netlink_pump();

  if (!netlink_link_up()) {
    ui_set_link(false);
  }

  lv_timer_handler();

  if (g_pending_cmd[0] != '\0') {
    netlink_send_cmd(g_pending_cmd, g_pending_cmd_x, g_pending_cmd_y);
    g_pending_cmd[0] = '\0';
    g_touch_event = false;
  } else if (g_touch_event) {
    g_touch_event = false;
    netlink_send_cmd("tap", g_last_touch_x, g_last_touch_y);
  }

  menu_ui_tick();
  delay(2);
}
