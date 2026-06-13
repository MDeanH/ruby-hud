// menu_ui.cpp — STATUS + MENU tiles (LVGL v8.3). See menu_ui.h.
//
// MENU is a stack of simple row-lists. A row is ACTION (stages a CMD verb,
// optional confirm modal), SUBMENU (pushes a child list), or INFO (live value
// only). The Ruby submenu's verbs ride the existing g_pending_cmd handoff in
// ui.cpp -> the .ino ships them to rubysat, which maps the allowlisted ones to
// the root updater. DISPLAY/CONNECTION/ABOUT do local actions or show info.

#include "menu_ui.h"
#include "netlink.h"
#include <Arduino.h>
#include <string.h>
#include <stdio.h>

// staged verb handoff (defined in ui.cpp)
extern char g_pending_cmd[24];
extern int  g_pending_cmd_x;
extern int  g_pending_cmd_y;

// local hooks implemented in the .ino / panel
extern void net_force_reconnect();
extern void panel_backlight(bool on);
extern void display_set_rotated(bool rot180);   // persists in NVS
extern bool display_is_rotated();
extern void display_set_mirror(bool on);        // windshield HUD flip (NVS)
extern bool display_is_mirrored();

#define FW_VERSION "3.4.2-sat"

// --- palette (mirror ui.cpp) ----------------------------------------------- //
#define M_BG      lv_color_hex(0x07090c)
#define M_PANEL   lv_color_hex(0x10141b)
#define M_BORDER  lv_color_hex(0x2a3340)
#define M_ACCENT  lv_color_hex(0xd0273b)
#define M_GLOW    lv_color_hex(0xff4d5c)
#define M_TEXT    lv_color_hex(0xf3f7fb)
#define M_DIM     lv_color_hex(0x8d99a7)
#define M_OK      lv_color_hex(0x2ecc71)
#define M_WARN    lv_color_hex(0xffb300)
#define M_DANGER  lv_color_hex(0xff3b30)

#define ROW_H 64
#define MAX_ROWS 8

enum RowType { ROW_ACTION, ROW_SUBMENU, ROW_INFO, ROW_BACK };

struct Row {
  RowType     type;
  const char *label;
  const char *verb;        // ACTION: CMD string staged into g_pending_cmd
  bool        confirm;     // ACTION: show modal first
  bool        danger;      // red label + red CONFIRM
  int         submenu;     // SUBMENU: index into g_menus
  const char *(*value_fn)();  // INFO/any: live right-aligned value
};

struct Menu {
  const char *title;
  Row         rows[MAX_ROWS];
  int         n;
};

// ------------------------------------------------------------------ values  //
static char s_ssid[40] = "--", s_myip[20] = "--", s_rubyip[20] = "--";
static int  s_rssi = 0;
static float s_rxrate = 0.f;
static char s_lastack[40] = "--";
static char s_buf[8][48];   // rotating scratch for value_fn returns

static const char *vf_ssid()   { return s_ssid; }
static const char *vf_myip()   { return s_myip; }
static const char *vf_rubyip() { return s_rubyip; }
static const char *vf_rssi()   { snprintf(s_buf[0], 48, "%d dBm", s_rssi); return s_buf[0]; }
static const char *vf_rxrate() { snprintf(s_buf[1], 48, "%.1f/s", s_rxrate); return s_buf[1]; }
static const char *vf_fw()     { return FW_VERSION; }
static const char *vf_build()  { return __DATE__; }
static const char *vf_heap()   { snprintf(s_buf[2], 48, "%u KB", (unsigned)(ESP.getFreeHeap()/1024)); return s_buf[2]; }
static const char *vf_psram()  { snprintf(s_buf[3], 48, "%u KB", (unsigned)(ESP.getFreePsram()/1024)); return s_buf[3]; }
static const char *vf_uptime() { unsigned s=millis()/1000; snprintf(s_buf[4],48,"%uh%02um",s/3600,(s%3600)/60); return s_buf[4]; }
static const char *vf_ack()    { return s_lastack; }
static const char *vf_rot()    { return display_is_rotated() ? "ON" : "off"; }
static const char *vf_mir()    { return display_is_mirrored() ? "ON" : "off"; }
static const char *vf_trans()  { return netlink_transport_label(); }
static const char *vf_netpref(){ return netlink_netpref_label(); }
static const char *vf_link()   {
  if (netlink_usb_active()) return "USB";
  if (netlink_wifi_up()) return "WIFI";
  return "down";
}

// ------------------------------------------------------------------ menus    //
// indices: 0 root, 1 RUBY, 2 DISPLAY, 3 CONNECTION, 4 ABOUT
static Menu g_menus[5] = {
  { "MENU", {
      { ROW_SUBMENU, "RUBY",       nullptr, false, false, 1, nullptr },
      { ROW_SUBMENU, "DISPLAY",    nullptr, false, false, 2, nullptr },
      { ROW_SUBMENU, "CONNECTION", nullptr, false, false, 3, nullptr },
      { ROW_SUBMENU, "ABOUT",      nullptr, false, false, 4, nullptr },
  }, 4 },
  { "RUBY", {
      { ROW_BACK,   "< back",       nullptr,            false, false, 0, nullptr },
      { ROW_ACTION, "Check updates","ruby_check",       false, false, 0, nullptr },
      { ROW_ACTION, "Update now",   "ruby_update",      true,  false, 0, nullptr },
      { ROW_ACTION, "Rollback",     "ruby_rollback",    true,  true,  0, nullptr },
      { ROW_ACTION, "Restart HUD",  "ruby_restart_hud", true,  false, 0, nullptr },
      { ROW_ACTION, "Console dash", "ruby_switch_dash", true,  false, 0, nullptr },
  }, 6 },
  { "DISPLAY", {
      { ROW_BACK,   "< back",        nullptr, false, false, 0, nullptr },
      { ROW_ACTION, "HUD Mirror",    "@mir",  false, false, 0, vf_mir },
      { ROW_ACTION, "Rotate 180",    "@rot",  false, false, 0, vf_rot },
      { ROW_ACTION, "Backlight test","@blt",  false, false, 0, nullptr },
  }, 4 },
  { "CONNECTION", {
      { ROW_BACK,   "< back",          nullptr,    false, false, 0, nullptr },
      { ROW_ACTION, "Transport",       "@tmode",   false, false, 0, vf_trans },
      { ROW_ACTION, "WiFi network",    "@nmode",   false, false, 0, vf_netpref },
      { ROW_INFO,   "Link",            nullptr,    false, false, 0, vf_link },
      { ROW_INFO,   "SSID",            nullptr,    false, false, 0, vf_ssid },
      { ROW_INFO,   "My IP",           nullptr,    false, false, 0, vf_myip },
      { ROW_INFO,   "Ruby IP",         nullptr,    false, false, 0, vf_rubyip },
      { ROW_INFO,   "RSSI",            nullptr,    false, false, 0, vf_rssi },
      { ROW_INFO,   "RX rate",         nullptr,    false, false, 0, vf_rxrate },
      { ROW_ACTION, "Sync WiFi from Pi","@wsync",  false, false, 0, nullptr },
      { ROW_ACTION, "Reconnect",       "@recon",   false, false, 0, nullptr },
  }, 10 },
  { "ABOUT", {
      { ROW_BACK, "< back",   nullptr, false, false, 0, nullptr },
      { ROW_INFO, "Firmware", nullptr, false, false, 0, vf_fw },
      { ROW_INFO, "Built",    nullptr, false, false, 0, vf_build },
      { ROW_INFO, "Heap",     nullptr, false, false, 0, vf_heap },
      { ROW_INFO, "PSRAM",    nullptr, false, false, 0, vf_psram },
      { ROW_INFO, "Uptime",   nullptr, false, false, 0, vf_uptime },
      { ROW_INFO, "Last verb",nullptr, false, false, 0, vf_ack },
  }, 7 },
};

// --------------------------------------------------------------- menu state  //
static lv_obj_t *s_menu_tile = nullptr;
static lv_obj_t *s_list = nullptr;          // current rows container
static int       s_cur = 0;                  // current menu index
static lv_obj_t *s_modal = nullptr;          // confirm overlay (or null)
static int       s_modal_menu = 0, s_modal_row = 0;
static lv_obj_t *s_toast = nullptr;
static uint32_t  s_toast_until = 0;

static void build_list(int menu_idx);

static void stage_verb(const char *verb) {
  strncpy(g_pending_cmd, verb, sizeof(g_pending_cmd) - 1);
  g_pending_cmd[sizeof(g_pending_cmd) - 1] = '\0';
  g_pending_cmd_x = 0; g_pending_cmd_y = 0;
}

static void show_toast(const char *msg) {
  if (s_toast) { lv_obj_del(s_toast); s_toast = nullptr; }
  s_toast = lv_label_create(lv_layer_top());
  lv_obj_set_style_bg_color(s_toast, M_PANEL, 0);
  lv_obj_set_style_bg_opa(s_toast, LV_OPA_COVER, 0);
  lv_obj_set_style_border_color(s_toast, M_ACCENT, 0);
  lv_obj_set_style_border_width(s_toast, 1, 0);
  lv_obj_set_style_radius(s_toast, 10, 0);
  lv_obj_set_style_pad_all(s_toast, 10, 0);
  lv_obj_set_style_text_color(s_toast, M_TEXT, 0);
  lv_obj_set_style_text_font(s_toast, &lv_font_montserrat_18, 0);
  lv_label_set_text(s_toast, msg);
  lv_obj_align(s_toast, LV_ALIGN_BOTTOM_MID, 0, -56);
  s_toast_until = millis() + 1600;
}

// ---- confirm modal -------------------------------------------------------- //
static void do_action(int menu_idx, int row_idx);

static void modal_cancel_cb(lv_event_t *e) {
  (void)e;
  if (s_modal) { lv_obj_del(s_modal); s_modal = nullptr; }
}
static void modal_confirm_cb(lv_event_t *e) {
  (void)e;
  int m = s_modal_menu, r = s_modal_row;
  if (s_modal) { lv_obj_del(s_modal); s_modal = nullptr; }
  Row &row = g_menus[m].rows[r];
  stage_verb(row.verb);
  show_toast("sent");
}

static void open_modal(int menu_idx, int row_idx) {
  Row &row = g_menus[menu_idx].rows[row_idx];
  s_modal_menu = menu_idx; s_modal_row = row_idx;
  s_modal = lv_obj_create(lv_layer_top());
  lv_obj_remove_style_all(s_modal);
  lv_obj_set_size(s_modal, 480, 480);
  lv_obj_set_style_bg_color(s_modal, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(s_modal, LV_OPA_60, 0);
  lv_obj_clear_flag(s_modal, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *card = lv_obj_create(s_modal);
  lv_obj_set_size(card, 380, 220);
  lv_obj_center(card);
  lv_obj_set_style_bg_color(card, M_PANEL, 0);
  lv_obj_set_style_border_color(card, row.danger ? M_DANGER : M_BORDER, 0);
  lv_obj_set_style_border_width(card, 2, 0);
  lv_obj_set_style_radius(card, 14, 0);
  lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *msg = lv_label_create(card);
  char buf[64]; snprintf(buf, 64, "%s ?", row.label);
  lv_label_set_text(msg, buf);
  lv_obj_set_style_text_color(msg, M_TEXT, 0);
  lv_obj_set_style_text_font(msg, &lv_font_montserrat_22, 0);
  lv_obj_align(msg, LV_ALIGN_TOP_MID, 0, 18);

  lv_obj_t *cancel = lv_btn_create(card);
  lv_obj_set_size(cancel, 150, 72);
  lv_obj_align(cancel, LV_ALIGN_BOTTOM_LEFT, 6, -6);
  lv_obj_set_style_bg_color(cancel, M_BORDER, 0);
  lv_obj_add_event_cb(cancel, modal_cancel_cb, LV_EVENT_CLICKED, nullptr);
  lv_obj_t *cl = lv_label_create(cancel); lv_label_set_text(cl, "CANCEL");
  lv_obj_center(cl);

  lv_obj_t *ok = lv_btn_create(card);
  lv_obj_set_size(ok, 150, 72);
  lv_obj_align(ok, LV_ALIGN_BOTTOM_RIGHT, -6, -6);
  lv_obj_set_style_bg_color(ok, row.danger ? M_DANGER : M_ACCENT, 0);
  lv_obj_add_event_cb(ok, modal_confirm_cb, LV_EVENT_CLICKED, nullptr);
  lv_obj_t *okl = lv_label_create(ok); lv_label_set_text(okl, "CONFIRM");
  lv_obj_center(okl);
}

static void do_action(int menu_idx, int row_idx) {
  Row &row = g_menus[menu_idx].rows[row_idx];
  if (row.verb && row.verb[0] == '@') {
    // local action
    if (!strcmp(row.verb, "@mir"))   { display_set_mirror(!display_is_mirrored()); show_toast("HUD mirror"); build_list(menu_idx); }
    else if (!strcmp(row.verb, "@rot"))   { display_set_rotated(!display_is_rotated()); show_toast("rotated"); build_list(menu_idx); }
    else if (!strcmp(row.verb, "@blt")) { panel_backlight(false); lv_timer_handler(); delay(250); panel_backlight(true); show_toast("backlight"); }
    else if (!strcmp(row.verb, "@recon")) { net_force_reconnect(); show_toast("reconnecting"); }
    else if (!strcmp(row.verb, "@tmode")) { netlink_cycle_transport(); show_toast(netlink_transport_label()); build_list(menu_idx); }
    else if (!strcmp(row.verb, "@nmode")) { netlink_cycle_netpref(); show_toast(netlink_netpref_label()); build_list(menu_idx); }
    else if (!strcmp(row.verb, "@wsync")) { netlink_request_wifi_sync(); show_toast("wifi sync"); }
    return;
  }
  if (row.confirm) { open_modal(menu_idx, row_idx); return; }
  if (row.verb) { stage_verb(row.verb); show_toast("sent"); }
}

// ---- row tap -------------------------------------------------------------- //
static void row_cb(lv_event_t *e) {
  if (s_modal) return;   // modal eats taps
  intptr_t packed = (intptr_t)lv_event_get_user_data(e);
  int menu_idx = (int)(packed >> 8);
  int row_idx  = (int)(packed & 0xFF);
  Row &row = g_menus[menu_idx].rows[row_idx];
  switch (row.type) {
    case ROW_BACK:    s_cur = row.submenu; build_list(s_cur); break;
    case ROW_SUBMENU: s_cur = row.submenu; build_list(s_cur); break;
    case ROW_ACTION:  do_action(menu_idx, row_idx); break;
    case ROW_INFO:    break;
  }
}

static void build_list(int menu_idx) {
  s_cur = menu_idx;
  if (s_list) { lv_obj_del(s_list); s_list = nullptr; }
  Menu &m = g_menus[menu_idx];

  s_list = lv_obj_create(s_menu_tile);
  lv_obj_remove_style_all(s_list);
  lv_obj_set_size(s_list, 480, 480);
  lv_obj_set_style_bg_color(s_list, M_BG, 0);
  lv_obj_set_style_bg_opa(s_list, LV_OPA_COVER, 0);
  lv_obj_set_flex_flow(s_list, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(s_list, 6, 0);
  lv_obj_set_style_pad_all(s_list, 10, 0);
  // vertical scroll only; never steal horizontal tile swipes
  lv_obj_set_scroll_dir(s_list, LV_DIR_VER);

  lv_obj_t *title = lv_label_create(s_list);
  lv_label_set_text(title, m.title);
  lv_obj_set_style_text_color(title, M_DIM, 0);
  lv_obj_set_style_text_font(title, &lv_font_montserrat_18, 0);

  for (int i = 0; i < m.n; i++) {
    Row &r = m.rows[i];
    lv_obj_t *row = lv_obj_create(s_list);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, 440, ROW_H);
    lv_obj_set_style_bg_color(row, M_PANEL, 0);
    lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(row, M_BORDER, 0);
    lv_obj_set_style_border_width(row, 1, 0);
    lv_obj_set_style_radius(row, 12, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    if (r.type != ROW_INFO) lv_obj_add_flag(row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(row, row_cb, LV_EVENT_CLICKED,
                        (void *)(intptr_t)((menu_idx << 8) | i));

    lv_obj_t *lbl = lv_label_create(row);
    lv_label_set_text(lbl, r.label);
    lv_obj_set_style_text_color(lbl, r.danger ? M_DANGER :
                                (r.type == ROW_INFO ? M_DIM : M_TEXT), 0);
    lv_obj_set_style_text_font(lbl, &lv_font_montserrat_22, 0);
    lv_obj_align(lbl, LV_ALIGN_LEFT_MID, 14, 0);

    if (r.value_fn) {
      lv_obj_t *val = lv_label_create(row);
      lv_label_set_text(val, r.value_fn());
      lv_obj_set_style_text_color(val, M_TEXT, 0);
      lv_obj_set_style_text_font(val, &lv_font_montserrat_18, 0);
      lv_obj_align(val, LV_ALIGN_RIGHT_MID, -14, 0);
    } else if (r.type == ROW_SUBMENU) {
      lv_obj_t *chev = lv_label_create(row);
      lv_label_set_text(chev, ">");
      lv_obj_set_style_text_color(chev, M_DIM, 0);
      lv_obj_set_style_text_font(chev, &lv_font_montserrat_22, 0);
      lv_obj_align(chev, LV_ALIGN_RIGHT_MID, -14, 0);
    }
  }
}

// --------------------------------------------------------------- STATUS tile //
static lv_obj_t *st_link, *st_can, *st_vis, *st_soc, *st_net;

static lv_obj_t *status_row(lv_obj_t *parent, const char *cap) {
  lv_obj_t *row = lv_obj_create(parent);
  lv_obj_remove_style_all(row);
  lv_obj_set_size(row, 440, 70);
  lv_obj_set_style_bg_color(row, M_PANEL, 0);
  lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
  lv_obj_set_style_border_color(row, M_BORDER, 0);
  lv_obj_set_style_border_width(row, 1, 0);
  lv_obj_set_style_radius(row, 12, 0);
  lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_t *c = lv_label_create(row);
  lv_label_set_text(c, cap);
  lv_obj_set_style_text_color(c, M_DIM, 0);
  lv_obj_set_style_text_font(c, &lv_font_montserrat_18, 0);
  lv_obj_align(c, LV_ALIGN_LEFT_MID, 14, 0);
  lv_obj_t *v = lv_label_create(row);
  lv_label_set_text(v, "--");
  lv_obj_set_style_text_color(v, M_TEXT, 0);
  lv_obj_set_style_text_font(v, &lv_font_montserrat_22, 0);
  lv_obj_align(v, LV_ALIGN_RIGHT_MID, -14, 0);
  return v;
}

void menu_ui_init(lv_obj_t *status_tile, lv_obj_t *menu_tile) {
  // STATUS tile
  lv_obj_set_style_bg_color(status_tile, M_BG, 0);
  lv_obj_set_style_bg_opa(status_tile, LV_OPA_COVER, 0);
  lv_obj_set_flex_flow(status_tile, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(status_tile, 8, 0);
  lv_obj_set_style_pad_all(status_tile, 16, 0);
  lv_obj_set_scroll_dir(status_tile, LV_DIR_VER);
  lv_obj_t *t = lv_label_create(status_tile);
  lv_label_set_text(t, "STATUS");
  lv_obj_set_style_text_color(t, M_DIM, 0);
  lv_obj_set_style_text_font(t, &lv_font_montserrat_18, 0);
  st_link = status_row(status_tile, "LINK");
  st_can  = status_row(status_tile, "CAN");
  st_vis  = status_row(status_tile, "VISION");
  st_soc  = status_row(status_tile, "SOC TEMP");
  st_net  = status_row(status_tile, "WIFI");

  // MENU tile
  s_menu_tile = menu_tile;
  lv_obj_clear_flag(menu_tile, LV_OBJ_FLAG_SCROLLABLE);
  build_list(0);
}

// --------------------------------------------------------------- setters     //
void menu_ui_set_net(const char *ssid, const char *my_ip, const char *ruby_ip,
                     int rssi_dbm, float rx_lines_s) {
  if (ssid)   { strncpy(s_ssid, ssid, sizeof(s_ssid)-1); s_ssid[sizeof(s_ssid)-1]=0; }
  if (my_ip)  { strncpy(s_myip, my_ip, sizeof(s_myip)-1); s_myip[sizeof(s_myip)-1]=0; }
  if (ruby_ip){ strncpy(s_rubyip, ruby_ip, sizeof(s_rubyip)-1); s_rubyip[sizeof(s_rubyip)-1]=0; }
  s_rssi = rssi_dbm; s_rxrate = rx_lines_s;
  if (st_net) {
    char b[48]; snprintf(b, 48, "%d dBm", s_rssi);
    lv_label_set_text(st_net, b);
  }
}

void menu_ui_set_state(const char *bus, int canfps, const char *vsrc,
                       int vdets, float soc_c, bool link_up) {
  if (st_link) {
    lv_label_set_text(st_link, link_up ? "LIVE" : "STALE");
    lv_obj_set_style_text_color(st_link, link_up ? M_OK : M_DANGER, 0);
  }
  if (st_can)  { char b[48]; snprintf(b,48,"%s %dfps", bus?bus:"--", canfps); lv_label_set_text(st_can,b); }
  if (st_vis)  { char b[48]; snprintf(b,48,"%s %d", vsrc?vsrc:"--", vdets); lv_label_set_text(st_vis,b); }
  if (st_soc)  { char b[48]; if (soc_c>0) snprintf(b,48,"%.0f C", soc_c); else snprintf(b,48,"--"); lv_label_set_text(st_soc,b); }
}

void menu_ui_set_ack(const char *ack) {
  if (!ack || !ack[0]) return;
  strncpy(s_lastack, ack, sizeof(s_lastack)-1); s_lastack[sizeof(s_lastack)-1]=0;
  show_toast(ack);
}

// called from the .ino loop to expire the toast
void menu_ui_tick() {
  if (s_toast && millis() > s_toast_until) {
    lv_obj_del(s_toast); s_toast = nullptr;
  }
}
