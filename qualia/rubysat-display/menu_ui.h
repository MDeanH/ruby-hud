// menu_ui.h — STATUS tile + MENU tile (stack menus w/ submenus, confirm
// modal, Ruby control verbs) for the rubysat satellite display.
//
// menu_ui_init(status_tile, menu_tile) builds both tiles' content.
// Verbs are staged through the same g_pending_cmd mechanism ui.cpp uses;
// the .ino ships them to rubysat as CMD lines.
#ifndef RUBYSAT_MENU_UI_H
#define RUBYSAT_MENU_UI_H

#include <lvgl.h>

void menu_ui_init(lv_obj_t *status_tile, lv_obj_t *menu_tile);

// 1 Hz-ish status refresh from the .ino (all strings ASCII, may be "").
void menu_ui_set_net(const char *ssid, const char *my_ip,
                     const char *ruby_ip, int rssi_dbm, float rx_lines_s);
void menu_ui_set_state(const char *bus, int canfps, const char *vsrc,
                       int vdets, float soc_c, bool link_up);
// Verb ack from the STATE stream ("<verb>:sent|failed"), shows a toast.
void menu_ui_set_ack(const char *ack);

#endif  // RUBYSAT_MENU_UI_H
