// ui.cpp — LVGL v8 gauge layout + setters for the rubysat display.
//
// Layout (720x720, origin top-left):
//   * Full-screen RPM arc gauge (0..8000), redline segment 6500+.
//   * Center: huge MPH number + "MPH" label.
//   * Below center: gear indicator (big glyph).
//   * Bottom row: four labeled mini-bars — COOL / VOLT / THR / FUEL.
//   * Top-left chip: CAN bus state + fps.  Top-right chip: vision src + dets.
//   * Top-center: connection dot (green=live, red=stale) + SoC temp.
//   * Invisible left/right tap zones emit page_prev / page_next CMDs.

#include "ui.h"
#include <lvgl.h>

// --- palette (LVGL colors) ------------------------------------------------- //
#define C_BG       lv_color_hex(0x07090c)
#define C_PANEL    lv_color_hex(0x10141b)
#define C_BORDER   lv_color_hex(0x2a3340)
#define C_TRACK    lv_color_hex(0x2a3340)
#define C_TRACKBR  lv_color_hex(0x4a5666)
#define C_ACCENT   lv_color_hex(0xd0273b)   // Soul Red
#define C_GLOW     lv_color_hex(0xff4d5c)
#define C_TEXT     lv_color_hex(0xf3f7fb)
#define C_DIM      lv_color_hex(0x8d99a7)
#define C_OK       lv_color_hex(0x2ecc71)
#define C_WARN     lv_color_hex(0xffb300)
#define C_DANGER   lv_color_hex(0xff3b30)

#define RPM_MAX     8000
#define RPM_REDLINE 6500

// --- command plumbing ------------------------------------------------------ //
char g_pending_cmd[16] = "";
int  g_pending_cmd_x = 0;
int  g_pending_cmd_y = 0;

// --- widget handles -------------------------------------------------------- //
static lv_obj_t *scr;
static lv_obj_t *arc_rpm;
static lv_obj_t *lbl_rpm;        // small numeric rpm under the arc
static lv_obj_t *lbl_mph;        // huge center number
static lv_obj_t *lbl_mph_unit;
static lv_obj_t *lbl_gear;

// mini-bars
static lv_obj_t *bar_cool, *bar_volt, *bar_thr, *bar_fuel;
static lv_obj_t *val_cool, *val_volt, *val_thr, *val_fuel;

// chips
static lv_obj_t *chip_can, *chip_can_lbl;
static lv_obj_t *chip_vis, *chip_vis_lbl;
static lv_obj_t *link_dot, *link_lbl;   // connection dot + SoC temp

// --- styles ---------------------------------------------------------------- //
static lv_style_t st_panel, st_chip;

static void make_styles() {
  lv_style_init(&st_panel);
  lv_style_set_bg_color(&st_panel, C_PANEL);
  lv_style_set_bg_opa(&st_panel, LV_OPA_COVER);
  lv_style_set_border_color(&st_panel, C_BORDER);
  lv_style_set_border_width(&st_panel, 1);
  lv_style_set_radius(&st_panel, 12);
  lv_style_set_pad_all(&st_panel, 8);

  lv_style_init(&st_chip);
  lv_style_set_bg_color(&st_chip, C_PANEL);
  lv_style_set_bg_opa(&st_chip, LV_OPA_COVER);
  lv_style_set_border_color(&st_chip, C_BORDER);
  lv_style_set_border_width(&st_chip, 1);
  lv_style_set_radius(&st_chip, 14);
  lv_style_set_pad_hor(&st_chip, 12);
  lv_style_set_pad_ver(&st_chip, 6);
}

// --- a labeled vertical-ish mini bar --------------------------------------- //
// Returns the bar; writes the value label handle to *out_val.
static lv_obj_t *make_minibar(lv_obj_t *parent, const char *name, int x,
                              lv_obj_t **out_val) {
  lv_obj_t *cont = lv_obj_create(parent);
  lv_obj_remove_style_all(cont);
  lv_obj_set_size(cont, 150, 120);
  lv_obj_set_pos(cont, x, 580);

  lv_obj_t *cap = lv_label_create(cont);
  lv_label_set_text(cap, name);
  lv_obj_set_style_text_color(cap, C_DIM, 0);
  lv_obj_set_style_text_font(cap, &lv_font_montserrat_18, 0);
  lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 0);

  lv_obj_t *bar = lv_bar_create(cont);
  lv_obj_set_size(bar, 130, 16);
  lv_obj_align(bar, LV_ALIGN_TOP_MID, 0, 30);
  lv_obj_set_style_bg_color(bar, C_TRACK, LV_PART_MAIN);
  lv_obj_set_style_bg_color(bar, C_ACCENT, LV_PART_INDICATOR);
  lv_obj_set_style_radius(bar, 8, LV_PART_MAIN);
  lv_obj_set_style_radius(bar, 8, LV_PART_INDICATOR);
  lv_bar_set_range(bar, 0, 100);
  lv_bar_set_value(bar, 0, LV_ANIM_OFF);

  lv_obj_t *val = lv_label_create(cont);
  lv_label_set_text(val, "--");
  lv_obj_set_style_text_color(val, C_TEXT, 0);
  lv_obj_set_style_text_font(val, &lv_font_montserrat_28, 0);
  lv_obj_align(val, LV_ALIGN_TOP_MID, 0, 56);

  *out_val = val;
  return bar;
}

// --- tap-zone event: emit page_prev / page_next ---------------------------- //
static void tapzone_cb(lv_event_t *e) {
  const char *cmd = (const char *)lv_event_get_user_data(e);
  lv_point_t p;
  lv_indev_t *indev = lv_indev_get_act();
  if (indev) lv_indev_get_point(indev, &p); else { p.x = 0; p.y = 0; }
  strncpy(g_pending_cmd, cmd, sizeof(g_pending_cmd) - 1);
  g_pending_cmd[sizeof(g_pending_cmd) - 1] = '\0';
  g_pending_cmd_x = p.x;
  g_pending_cmd_y = p.y;
}

void ui_init() {
  make_styles();

  scr = lv_scr_act();
  lv_obj_set_style_bg_color(scr, C_BG, 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

  // ---- RPM arc (full-screen ring) ----
  arc_rpm = lv_arc_create(scr);
  lv_obj_set_size(arc_rpm, 700, 700);
  lv_obj_center(arc_rpm);
  lv_arc_set_rotation(arc_rpm, 135);
  lv_arc_set_bg_angles(arc_rpm, 0, 270);
  lv_arc_set_range(arc_rpm, 0, RPM_MAX);
  lv_arc_set_value(arc_rpm, 0);
  lv_obj_remove_style(arc_rpm, NULL, LV_PART_KNOB);          // no drag knob
  lv_obj_clear_flag(arc_rpm, LV_OBJ_FLAG_CLICKABLE);         // display only
  lv_obj_set_style_arc_color(arc_rpm, C_TRACK, LV_PART_MAIN);
  lv_obj_set_style_arc_width(arc_rpm, 22, LV_PART_MAIN);
  lv_obj_set_style_arc_color(arc_rpm, C_ACCENT, LV_PART_INDICATOR);
  lv_obj_set_style_arc_width(arc_rpm, 22, LV_PART_INDICATOR);

  // small numeric rpm readout near bottom of the ring
  lbl_rpm = lv_label_create(scr);
  lv_label_set_text(lbl_rpm, "---- RPM");
  lv_obj_set_style_text_color(lbl_rpm, C_DIM, 0);
  lv_obj_set_style_text_font(lbl_rpm, &lv_font_montserrat_28, 0);
  lv_obj_align(lbl_rpm, LV_ALIGN_CENTER, 0, 150);

  // ---- center MPH ----
  lbl_mph = lv_label_create(scr);
  lv_label_set_text(lbl_mph, "--");
  lv_obj_set_style_text_color(lbl_mph, C_TEXT, 0);
  // Montserrat 48 is the largest stock LVGL font. It's enabled in lv_conf.h and
  // reads cleanly at arm's length without bitmap upscaling. (For an even larger
  // numeral, build a custom font with lv_font_conv and swap it in here.)
  lv_obj_set_style_text_font(lbl_mph, &lv_font_montserrat_48, 0);
  lv_obj_align(lbl_mph, LV_ALIGN_CENTER, 0, -40);

  lbl_mph_unit = lv_label_create(scr);
  lv_label_set_text(lbl_mph_unit, "MPH");
  lv_obj_set_style_text_color(lbl_mph_unit, C_DIM, 0);
  lv_obj_set_style_text_font(lbl_mph_unit, &lv_font_montserrat_28, 0);
  lv_obj_align(lbl_mph_unit, LV_ALIGN_CENTER, 0, 40);

  // ---- gear ----
  lbl_gear = lv_label_create(scr);
  lv_label_set_text(lbl_gear, "-");
  lv_obj_set_style_text_color(lbl_gear, C_GLOW, 0);
  lv_obj_set_style_text_font(lbl_gear, &lv_font_montserrat_48, 0);
  lv_obj_align(lbl_gear, LV_ALIGN_CENTER, 0, 95);

  // ---- mini-bars (bottom row) ----
  bar_cool = make_minibar(scr, "COOL", 30,  &val_cool);
  bar_volt = make_minibar(scr, "VOLT", 200, &val_volt);
  bar_thr  = make_minibar(scr, "THR",  370, &val_thr);
  bar_fuel = make_minibar(scr, "FUEL", 540, &val_fuel);

  // ---- CAN chip (top-left) ----
  chip_can = lv_obj_create(scr);
  lv_obj_remove_style_all(chip_can);
  lv_obj_add_style(chip_can, &st_chip, 0);
  lv_obj_set_size(chip_can, 180, 44);
  lv_obj_align(chip_can, LV_ALIGN_TOP_LEFT, 16, 16);
  chip_can_lbl = lv_label_create(chip_can);
  lv_label_set_text(chip_can_lbl, "CAN --");
  lv_obj_set_style_text_color(chip_can_lbl, C_TEXT, 0);
  lv_obj_set_style_text_font(chip_can_lbl, &lv_font_montserrat_22, 0);
  lv_obj_center(chip_can_lbl);

  // ---- vision chip (top-right) ----
  chip_vis = lv_obj_create(scr);
  lv_obj_remove_style_all(chip_vis);
  lv_obj_add_style(chip_vis, &st_chip, 0);
  lv_obj_set_size(chip_vis, 190, 44);
  lv_obj_align(chip_vis, LV_ALIGN_TOP_RIGHT, -16, 16);
  chip_vis_lbl = lv_label_create(chip_vis);
  lv_label_set_text(chip_vis_lbl, "VIS off");
  lv_obj_set_style_text_color(chip_vis_lbl, C_TEXT, 0);
  lv_obj_set_style_text_font(chip_vis_lbl, &lv_font_montserrat_22, 0);
  lv_obj_center(chip_vis_lbl);

  // ---- connection dot + SoC temp (top-center) ----
  link_dot = lv_obj_create(scr);
  lv_obj_remove_style_all(link_dot);
  lv_obj_set_size(link_dot, 22, 22);
  lv_obj_set_style_radius(link_dot, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_bg_opa(link_dot, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(link_dot, C_DANGER, 0);
  lv_obj_align(link_dot, LV_ALIGN_TOP_MID, -34, 24);

  link_lbl = lv_label_create(scr);
  lv_label_set_text(link_lbl, "--C");
  lv_obj_set_style_text_color(link_lbl, C_DIM, 0);
  lv_obj_set_style_text_font(link_lbl, &lv_font_montserrat_22, 0);
  lv_obj_align(link_lbl, LV_ALIGN_TOP_MID, 18, 24);

  // ---- invisible page tap-zones (left/right edges) ----
  lv_obj_t *zl = lv_obj_create(scr);
  lv_obj_remove_style_all(zl);
  lv_obj_set_size(zl, 90, 460);
  lv_obj_align(zl, LV_ALIGN_LEFT_MID, 0, 0);
  lv_obj_add_flag(zl, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_event_cb(zl, tapzone_cb, LV_EVENT_CLICKED, (void *)"page_prev");

  lv_obj_t *zr = lv_obj_create(scr);
  lv_obj_remove_style_all(zr);
  lv_obj_set_size(zr, 90, 460);
  lv_obj_align(zr, LV_ALIGN_RIGHT_MID, 0, 0);
  lv_obj_add_flag(zr, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_event_cb(zr, tapzone_cb, LV_EVENT_CLICKED, (void *)"page_next");
}

// --- helpers --------------------------------------------------------------- //
static void set_bar(lv_obj_t *bar, lv_obj_t *val, int v, const char *suffix,
                    bool warn) {
  if (v <= -1000 || v < 0) {                  // null sentinel
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    lv_label_set_text(val, "--");
    return;
  }
  int clamped = v;
  if (clamped < 0) clamped = 0;
  if (clamped > 100) clamped = 100;
  lv_bar_set_value(bar, clamped, LV_ANIM_OFF);
  static char buf[16];
  snprintf(buf, sizeof(buf), "%d%s", v, suffix);
  lv_label_set_text(val, buf);
  lv_obj_set_style_bg_color(bar, warn ? C_WARN : C_ACCENT, LV_PART_INDICATOR);
}

void ui_update(const StateView &s) {
  // RPM arc + numeric.
  if (s.rpm >= 0) {
    int r = s.rpm; if (r > RPM_MAX) r = RPM_MAX;
    lv_arc_set_value(arc_rpm, r);
    lv_obj_set_style_arc_color(
        arc_rpm, (s.rpm >= RPM_REDLINE) ? C_DANGER : C_ACCENT,
        LV_PART_INDICATOR);
    static char rb[24];
    snprintf(rb, sizeof(rb), "%d RPM", s.rpm);
    lv_label_set_text(lbl_rpm, rb);
  } else {
    lv_arc_set_value(arc_rpm, 0);
    lv_label_set_text(lbl_rpm, "---- RPM");
  }

  // Speed.
  if (s.mph >= 0) {
    static char mb[8];
    snprintf(mb, sizeof(mb), "%d", s.mph);
    lv_label_set_text(lbl_mph, mb);
  } else {
    lv_label_set_text(lbl_mph, "--");
  }

  // Gear.
  lv_label_set_text(lbl_gear, s.gear[0] ? s.gear : "-");

  // Coolant: scale C onto a 0..100-ish bar (40C..120C window), warn >110.
  if (s.coolant > -1000) {
    int pct = (int)((s.coolant - 40) * 100.0 / 80.0);  // 40..120C -> 0..100
    if (pct < 0) pct = 0; if (pct > 100) pct = 100;
    lv_bar_set_value(bar_cool, pct, LV_ANIM_OFF);
    static char cb[16];
    snprintf(cb, sizeof(cb), "%dC", s.coolant);
    lv_label_set_text(val_cool, cb);
    lv_obj_set_style_bg_color(bar_cool,
        (s.coolant > 110) ? C_DANGER : C_ACCENT, LV_PART_INDICATOR);
  } else {
    lv_bar_set_value(bar_cool, 0, LV_ANIM_OFF);
    lv_label_set_text(val_cool, "--");
  }

  // Volts: window 10..15V -> 0..100; warn < 11.8V.
  if (s.volts >= 0.0f) {
    int pct = (int)((s.volts - 10.0f) * 100.0f / 5.0f);
    if (pct < 0) pct = 0; if (pct > 100) pct = 100;
    lv_bar_set_value(bar_volt, pct, LV_ANIM_OFF);
    static char vb[16];
    snprintf(vb, sizeof(vb), "%.1f", s.volts);
    lv_label_set_text(val_volt, vb);
    lv_obj_set_style_bg_color(bar_volt,
        (s.volts < 11.8f) ? C_WARN : C_ACCENT, LV_PART_INDICATOR);
  } else {
    lv_bar_set_value(bar_volt, 0, LV_ANIM_OFF);
    lv_label_set_text(val_volt, "--");
  }

  // Throttle / fuel: already percent.
  set_bar(bar_thr,  val_thr,  s.throttle, "%", false);
  set_bar(bar_fuel, val_fuel, s.fuel,     "%", (s.fuel >= 0 && s.fuel < 12));

  // CAN chip. ui_update runs on every STATE line (~15 Hz); every set_text /
  // set_style marks the object dirty and forces a re-render of that region, but
  // the chip text/colour change far slower than 15 Hz. Cache the last-pushed
  // string + colour and skip the (heavy) snprintf/set_text/set_style when
  // unchanged -- this is the costliest per-frame UI work after the RPM arc.
  {
    char canb[32];
    snprintf(canb, sizeof(canb), "CAN %s  %d", s.bus, s.canfps);
    lv_color_t canc = C_OK;
    if (strcmp(s.bus, "ERROR") == 0) canc = C_DANGER;
    else if (strcmp(s.bus, "UP") != 0) canc = C_WARN;
    static char canb_prev[32] = {0};
    if (strncmp(canb, canb_prev, sizeof(canb)) != 0) {
      strncpy(canb_prev, canb, sizeof(canb_prev));
      lv_label_set_text(chip_can_lbl, canb);
    }
    static uint32_t canc_prev = 0xFFFFFFFFu;
    if (lv_color_to32(canc) != canc_prev) {
      canc_prev = lv_color_to32(canc);
      lv_obj_set_style_border_color(chip_can, canc, 0);
    }
  }

  // Vision chip. Same change-detection rationale as the CAN chip.
  {
    char visb[32];
    snprintf(visb, sizeof(visb), "VIS %s  %d", s.vsrc, s.vdets);
    lv_color_t visc = (strcmp(s.vsrc, "off") == 0) ? C_DIM : C_OK;
    static char visb_prev[32] = {0};
    if (strncmp(visb, visb_prev, sizeof(visb)) != 0) {
      strncpy(visb_prev, visb, sizeof(visb_prev));
      lv_label_set_text(chip_vis_lbl, visb);
    }
    static uint32_t visc_prev = 0xFFFFFFFFu;
    if (lv_color_to32(visc) != visc_prev) {
      visc_prev = lv_color_to32(visc);
      lv_obj_set_style_border_color(chip_vis, visc, 0);
    }
  }

  // SoC temp text.
  if (s.soc > -1000.0f) {
    static char sb[12];
    snprintf(sb, sizeof(sb), "%dC", (int)(s.soc + 0.5f));
    lv_label_set_text(link_lbl, sb);
    lv_obj_set_style_text_color(link_lbl,
        (s.soc > 80.0f) ? C_DANGER : C_DIM, 0);
  }
}

void ui_set_link(bool ok) {
  if (!link_dot) return;
  lv_obj_set_style_bg_color(link_dot, ok ? C_OK : C_DANGER, 0);
}
