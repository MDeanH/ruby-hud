// ui.h — LVGL gauge UI for the rubysat display (720x720 square, dark HUD theme).
//
// Palette matches the rubyhud "premium cluster" theme:
//   bg #07090c, panel #10141b, accent Soul Red #d0273b, text #f3f7fb,
//   dim text #8d99a7, ok #2ecc71, warn #ffb300, danger #ff3b30.

#ifndef RUBYSAT_UI_H
#define RUBYSAT_UI_H

#include <Arduino.h>

// Decoded STATE line, passed from the .ino parser to the UI.
// Sentinels used for "null" so the UI can blank a field to "--":
//   int   fields: -1        (coolant uses -1000)
//   float fields: -1.0      (soc uses -1000.0)
//
// gear/bus/vsrc are fixed char buffers (NOT Arduino String): this struct is
// rebuilt for every STATE line at ~15 Hz, and String members would heap-alloc
// 45 times/sec on a long-running embedded target (slow heap fragmentation).
// Fixed buffers are filled with strlcpy in the .ino parser. Sizes cover the
// longest documented values ("NO BUS"=6, "pattern"=7) plus the NUL.
struct StateView {
  int   rpm;        // -1 => null
  int   mph;        // -1 => null
  char  gear[8];    // "-" when unknown
  int   coolant;    // degrees C; -1000 => null
  float volts;      // -1.0 => null
  int   throttle;   // %; -1 => null
  int   fuel;       // %; -1 => null
  char  bus[8];     // "UP" | "NO BUS" | "ERROR"
  int   canfps;
  char  vsrc[8];    // csi | usb | video | pattern | off
  int   vdets;
  float soc;        // SoC temp C; -1000.0 => null
};

// Touch/command plumbing shared with the .ino loop.
extern char    g_pending_cmd[16];   // "" when none; e.g. "page_next"
extern int     g_pending_cmd_x;
extern int     g_pending_cmd_y;

// Build the screen once.
void ui_init();

// Push a fresh STATE into the gauges.
void ui_update(const StateView &s);

// Connection chip: true = receiving fresh state (green), false = stale/down(red)
void ui_set_link(bool ok);

#endif  // RUBYSAT_UI_H
