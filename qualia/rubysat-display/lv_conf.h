// lv_conf.h — LVGL v8 configuration for the rubysat Qualia display.
//
// Minimal config derived from the upstream lv_conf_template.h for LVGL v8.x.
// Tuned for: RGB565 (LV_COLOR_DEPTH 16), ESP32-S3 with octal PSRAM, the
// Montserrat font sizes the UI uses (18/22/28/48), arc + bar widgets.
//
// IMPORTANT for Arduino IDE / arduino-cli: this file must be visible to the
// LVGL library. Two supported options:
//   1) Set the LVGL "configuration" via the build flag -DLV_CONF_INCLUDE_SIMPLE
//      and keep this lv_conf.h next to the sketch (it's on the include path).
//   2) Or copy this file to the Arduino libraries/ root (one level ABOVE the
//      lvgl/ folder) per the LVGL Arduino install note.
// platformio.ini already passes -DLV_CONF_INCLUDE_SIMPLE for path option (1).

#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

/*====================
   COLOR SETTINGS
 *====================*/
#define LV_COLOR_DEPTH 16
#define LV_COLOR_16_SWAP 0          /* Arduino_GFX draw16bitRGBBitmap wants
                                       native byte order; no swap. */
#define LV_COLOR_SCREEN_TRANSP 0
#define LV_COLOR_MIX_ROUND_OFS 0
#define LV_COLOR_CHROMA_KEY lv_color_hex(0x00ff00)

/*=========================
   MEMORY SETTINGS
 *=========================*/
/* LVGL's own allocator. 64KB is comfortable for this UI; it lives in internal
   RAM (the big draw buffer is allocated separately in PSRAM by the sketch). */
#define LV_MEM_CUSTOM 0
#if LV_MEM_CUSTOM == 0
  #define LV_MEM_SIZE (64U * 1024U)
  #define LV_MEM_ADR 0
#else
  #define LV_MEM_CUSTOM_INCLUDE <stdlib.h>
  #define LV_MEM_CUSTOM_ALLOC   malloc
  #define LV_MEM_CUSTOM_FREE    free
  #define LV_MEM_CUSTOM_REALLOC realloc
#endif
#define LV_MEM_BUF_MAX_NUM 16
#define LV_MEMCPY_MEMSET_STD 0

/*====================
   HAL SETTINGS
 *====================*/
#define LV_DISP_DEF_REFR_PERIOD 16   /* ~60Hz refresh attempt */
#define LV_INDEV_DEF_READ_PERIOD 30  /* touch poll period (ms) */
#define LV_TICK_CUSTOM 0             /* we drive lv_tick_inc from a HW timer */
#if LV_TICK_CUSTOM
  #define LV_TICK_CUSTOM_INCLUDE "Arduino.h"
  #define LV_TICK_CUSTOM_SYS_TIME_EXPR (millis())
#endif
#define LV_DPI_DEF 130               /* 4" 720px square ~ high DPI */

/*=======================
   FEATURE CONFIGURATION
 *=======================*/
#define LV_USE_PERF_MONITOR 0
#define LV_USE_MEM_MONITOR 0
#define LV_USE_REFR_DEBUG 0
#define LV_USE_ASSERT_NULL 1
#define LV_USE_ASSERT_MALLOC 1
#define LV_USE_ASSERT_STYLE 0
#define LV_USE_ASSERT_MEM_INTEGRITY 0
#define LV_USE_ASSERT_OBJ 0
#define LV_USE_LOG 0
#define LV_USE_USER_DATA 1

/* Drawing engine: stock software renderer. */
#define LV_DRAW_COMPLEX 1
#define LV_SHADOW_CACHE_SIZE 0
#define LV_CIRCLE_CACHE_SIZE 4
#define LV_LAYER_SIMPLE_BUF_SIZE (24U * 1024U)
#define LV_IMG_CACHE_DEF_SIZE 0
#define LV_GRADIENT_MAX_STOPS 2
#define LV_GRAD_CACHE_DEF_SIZE 0
#define LV_DITHER_GRADIENT 0
#define LV_DISP_ROT_MAX_BUF (10U * 1024U)

/*==================
 *   FONT USAGE
 *===================*/
#define LV_FONT_MONTSERRAT_8  0
#define LV_FONT_MONTSERRAT_10 0
#define LV_FONT_MONTSERRAT_12 0
#define LV_FONT_MONTSERRAT_14 1     /* LVGL default font */
#define LV_FONT_MONTSERRAT_16 0
#define LV_FONT_MONTSERRAT_18 1     /* mini-bar captions */
#define LV_FONT_MONTSERRAT_20 0
#define LV_FONT_MONTSERRAT_22 1     /* chips / SoC temp */
#define LV_FONT_MONTSERRAT_24 0
#define LV_FONT_MONTSERRAT_26 0
#define LV_FONT_MONTSERRAT_28 1     /* unit + bar values */
#define LV_FONT_MONTSERRAT_30 0
#define LV_FONT_MONTSERRAT_32 0
#define LV_FONT_MONTSERRAT_34 0
#define LV_FONT_MONTSERRAT_36 0
#define LV_FONT_MONTSERRAT_38 0
#define LV_FONT_MONTSERRAT_40 0
#define LV_FONT_MONTSERRAT_42 0
#define LV_FONT_MONTSERRAT_44 0
#define LV_FONT_MONTSERRAT_46 0
#define LV_FONT_MONTSERRAT_48 1     /* big speed + gear (transform-scaled) */

#define LV_FONT_MONTSERRAT_12_SUBPX 0
#define LV_FONT_MONTSERRAT_28_COMPRESSED 0
#define LV_FONT_DEJAVU_16_PERSIAN_HEBREW 0
#define LV_FONT_SIMSUN_16_CJK 0
#define LV_FONT_UNSCII_8 0
#define LV_FONT_UNSCII_16 0
#define LV_FONT_CUSTOM_DECLARE
#define LV_FONT_DEFAULT &lv_font_montserrat_14
#define LV_FONT_FMT_TXT_LARGE 0
#define LV_USE_FONT_COMPRESSED 0
#define LV_USE_FONT_SUBPX 0
#define LV_USE_FONT_PLACEHOLDER 1

/*=================
 *  TEXT SETTINGS
 *=================*/
#define LV_TXT_ENC LV_TXT_ENC_UTF8
#define LV_TXT_BREAK_CHARS " ,.;:-_"
#define LV_TXT_LINE_BREAK_LONG_LEN 0
#define LV_TXT_LINE_BREAK_LONG_PRE_MIN_LEN 3
#define LV_TXT_LINE_BREAK_LONG_POST_MIN_LEN 3
#define LV_TXT_COLOR_CMD "#"
#define LV_USE_BIDI 0
#define LV_USE_ARABIC_PERSIAN_CHARS 0

/*==================
 *  WIDGET USAGE
 *================*/
#define LV_USE_ARC 1
#define LV_USE_BAR 1
#define LV_USE_BTN 1
#define LV_USE_BTNMATRIX 0
#define LV_USE_CANVAS 0
#define LV_USE_CHECKBOX 0
#define LV_USE_DROPDOWN 0
#define LV_USE_IMG 1
#define LV_USE_LABEL 1
#if LV_USE_LABEL
  #define LV_LABEL_TEXT_SELECTION 0
  #define LV_LABEL_LONG_TXT_HINT 1
#endif
#define LV_USE_LINE 1
#define LV_USE_ROLLER 0
#define LV_USE_SLIDER 0
#define LV_USE_SWITCH 0
#define LV_USE_TEXTAREA 0
#define LV_USE_TABLE 0

/*==================
 * EXTRA COMPONENTS
 *==================*/
#define LV_USE_ANIMIMG 0
#define LV_USE_CALENDAR 0
#define LV_USE_CHART 0
#define LV_USE_COLORWHEEL 0
#define LV_USE_IMGBTN 0
#define LV_USE_KEYBOARD 0
#define LV_USE_LED 0
#define LV_USE_LIST 0
#define LV_USE_MENU 0
#define LV_USE_METER 0
#define LV_USE_MSGBOX 0
#define LV_USE_SPAN 0
#define LV_USE_SPINBOX 0
#define LV_USE_SPINNER 1
#define LV_USE_TABVIEW 0
#define LV_USE_TILEVIEW 0
#define LV_USE_WIN 0

/*-----------
 * Themes
 *----------*/
#define LV_USE_THEME_DEFAULT 1
#if LV_USE_THEME_DEFAULT
  #define LV_THEME_DEFAULT_DARK 1
  #define LV_THEME_DEFAULT_GROW 0
  #define LV_THEME_DEFAULT_TRANSITION_TIME 80
#endif
#define LV_USE_THEME_BASIC 0
#define LV_USE_THEME_MONO 0

/*-----------
 * Layouts
 *----------*/
#define LV_USE_FLEX 1
#define LV_USE_GRID 1

/*---------------------
 * 3rd party libraries
 *--------------------*/
#define LV_USE_FS_STDIO 0
#define LV_USE_FS_POSIX 0
#define LV_USE_FS_WIN32 0
#define LV_USE_FS_FATFS 0
#define LV_USE_PNG 0
#define LV_USE_BMP 0
#define LV_USE_SJPG 0
#define LV_USE_GIF 0
#define LV_USE_QRCODE 0
#define LV_USE_FREETYPE 0
#define LV_USE_RLOTTIE 0
#define LV_USE_FFMPEG 0

/*-----------
 * Others
 *----------*/
#define LV_USE_SNAPSHOT 0
#define LV_USE_MONKEY 0
#define LV_USE_GRIDNAV 0
#define LV_USE_FRAGMENT 0
#define LV_USE_IMGFONT 0
#define LV_USE_MSG 0
#define LV_USE_IME_PINYIN 0

/*==================
 * EXAMPLES / DEMOS
 *==================*/
#define LV_BUILD_EXAMPLES 0

#endif /*LV_CONF_H*/
