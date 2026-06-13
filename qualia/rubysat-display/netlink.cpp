// netlink.cpp — USB CDC + Wi-Fi TCP transport for rubysat satellite.

#include "netlink.h"
#include "secrets.h"

#include <WiFi.h>
#include <ESPmDNS.h>
#include <WiFiClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>

static WiFiClient s_sock;
static Preferences s_prefs;

static TransportMode s_transport = TRANS_AUTO;
static NetPref       s_netpref = NET_PRIMARY;

static char s_pi_ssid[40] = "";
static char s_pi_pass[64] = "";

static IPAddress s_ruby_ip;
static bool s_have_ruby_ip = false;

static uint32_t s_last_state_ms = 0;
static uint32_t s_last_connect_try = 0;
static uint32_t s_connect_backoff = 500;
static const uint32_t BACKOFF_MAX = 8000;
static const uint32_t USB_PROBE_MS = 2500;

static String s_rxbuf;
static bool s_usb_seen = false;
static bool s_using_usb = false;
static uint32_t s_boot_ms = 0;

static bool s_wifi_on_secondary = false;
static uint32_t s_wifi_started = 0;

// --------------------------------------------------------------------------- //
static void wifi_apply_pref() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  const char *ssid = WIFI_SSID;
  const char *pass = WIFI_PASS;
  if (s_netpref == NET_SECONDARY && strlen(WIFI_SSID2) > 0) {
    ssid = WIFI_SSID2;
    pass = WIFI_PASS2;
    s_wifi_on_secondary = true;
  } else if (s_netpref == NET_FROM_PI && s_pi_ssid[0]) {
    ssid = s_pi_ssid;
    pass = s_pi_pass;
    s_wifi_on_secondary = false;
  } else {
    s_wifi_on_secondary = false;
  }
  WiFi.begin(ssid, pass);
  s_wifi_started = millis();
}

static void wifi_begin() {
  wifi_apply_pref();
}

static bool wifi_pump() {
  if (WiFi.status() == WL_CONNECTED) return true;
  uint32_t now = millis();
  if (s_wifi_started == 0) s_wifi_started = now;
  if ((now - s_wifi_started) > 12000) {
    if (strlen(WIFI_SSID2) > 0 && s_netpref == NET_PRIMARY) {
      s_wifi_on_secondary = !s_wifi_on_secondary;
      if (s_wifi_on_secondary) WiFi.begin(WIFI_SSID2, WIFI_PASS2);
      else                     WiFi.begin(WIFI_SSID, WIFI_PASS);
    } else {
      wifi_apply_pref();
    }
    s_wifi_started = now;
  }
  return false;
}

static void resolve_ruby() {
  if (s_have_ruby_ip) return;
  static bool mdns_up = false;
  if (!mdns_up) {
    if (MDNS.begin("rubysat")) mdns_up = true;
  }
  if (mdns_up) {
    IPAddress ip = MDNS.queryHost("ruby");
    if (ip != IPAddress((uint32_t)0)) {
      s_ruby_ip = ip;
      s_have_ruby_ip = true;
      return;
    }
  }
  if (s_ruby_ip.fromString(RUBY_FALLBACK_IP)) {
    s_have_ruby_ip = true;
  }
}

static void handle_wifi_line(const String &line) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, line)) return;
  if (strcmp(doc["type"] | "", "wifi") != 0) return;
  const char *ssid = doc["ssid"] | "";
  const char *pass = doc["pass"] | "";
  if (!ssid[0]) return;
  strlcpy(s_pi_ssid, ssid, sizeof(s_pi_ssid));
  strlcpy(s_pi_pass, pass, sizeof(s_pi_pass));
  s_prefs.putString("pi_ssid", s_pi_ssid);
  s_prefs.putString("pi_pass", s_pi_pass);
  s_netpref = NET_FROM_PI;
  s_prefs.putUChar("netpref", s_netpref);
  wifi_apply_pref();
}

static void consume_line(const String &line, bool from_usb) {
  if (line.length() < 2) return;
  if (line.startsWith("{\"type\":\"wifi\"")) {
    handle_wifi_line(line);
    return;
  }
  if (line.charAt(0) != '{') return;
  if (from_usb) s_usb_seen = true;
  netlink_on_state_line(line);
  s_last_state_ms = millis();
}

static void drain_stream(Stream &io, bool from_usb) {
  while (io.available() > 0) {
    char c = (char)io.read();
    if (c == '\n') {
      consume_line(s_rxbuf, from_usb);
      s_rxbuf = "";
    } else if (c != '\r') {
      if (s_rxbuf.length() < 1024) s_rxbuf += c;
      else s_rxbuf = "";
    }
  }
}

static void tcp_pump() {
  if (WiFi.status() != WL_CONNECTED) {
    if (s_sock.connected()) s_sock.stop();
    return;
  }
  if (!s_sock.connected()) {
    uint32_t now = millis();
    if (now - s_last_connect_try < s_connect_backoff) return;
    s_last_connect_try = now;
    resolve_ruby();
    if (!s_have_ruby_ip) return;
    if (s_sock.connect(s_ruby_ip, RUBY_PORT, 1500)) {
      s_sock.setNoDelay(true);
      s_connect_backoff = 500;
      s_rxbuf = "";
    } else {
      s_have_ruby_ip = false;
      s_connect_backoff *= 2;
      if (s_connect_backoff > BACKOFF_MAX) s_connect_backoff = BACKOFF_MAX;
    }
    return;
  }
  drain_stream(s_sock, false);
}

static bool usb_want() {
  if (s_transport == TRANS_USB) return true;
  if (s_transport == TRANS_WIFI) return false;
  // AUTO: prefer USB until probe window expires without data, then Wi-Fi.
  if (s_usb_seen) return true;
  if (millis() - s_boot_ms < USB_PROBE_MS) return true;
  return false;
}

static void usb_pump() {
  drain_stream(Serial, true);
}

static bool write_cmd(const char *payload, size_t n) {
  if (s_using_usb) {
    return Serial.write((const uint8_t *)payload, n) == n;
  }
  if (s_sock.connected()) {
    return s_sock.write((const uint8_t *)payload, n) == n;
  }
  return false;
}

// --------------------------------------------------------------------------- //
// Public API
// --------------------------------------------------------------------------- //
void netlink_begin() {
  s_boot_ms = millis();
  s_prefs.begin("rubysat", false);
  s_transport = (TransportMode)s_prefs.getUChar("transport", TRANS_AUTO);
  s_netpref = (NetPref)s_prefs.getUChar("netpref", NET_PRIMARY);
  s_prefs.getString("pi_ssid", s_pi_ssid, sizeof(s_pi_ssid));
  s_prefs.getString("pi_pass", s_pi_pass, sizeof(s_pi_pass));
  if (s_netpref == NET_FROM_PI && !s_pi_ssid[0]) {
    s_netpref = NET_PRIMARY;
  }
  wifi_begin();
}

void netlink_pump() {
  bool want_usb = usb_want();

  if (want_usb) {
    usb_pump();
    if (s_usb_seen || s_transport == TRANS_USB) {
      s_using_usb = true;
      if (s_sock.connected()) s_sock.stop();
      return;
    }
  }

  s_using_usb = false;
  if (s_transport != TRANS_USB) {
    wifi_pump();
    tcp_pump();
  }
}

void netlink_send_cmd(const char *cmd, int x, int y) {
  StaticJsonDocument<96> doc;
  doc["cmd"] = cmd;
  doc["x"] = x;
  doc["y"] = y;
  char out[96];
  size_t n = serializeJson(doc, out, sizeof(out));
  if (n == 0 || n >= sizeof(out) - 1) return;
  out[n] = '\n';
  write_cmd(out, n + 1);
}

void netlink_force_reconnect() {
  if (s_sock.connected()) s_sock.stop();
  s_have_ruby_ip = false;
  s_connect_backoff = 500;
  s_last_connect_try = 0;
  s_last_state_ms = 0;
  s_usb_seen = false;
  s_boot_ms = millis();
  wifi_apply_pref();
}

TransportMode netlink_transport() { return s_transport; }
NetPref netlink_netpref() { return s_netpref; }

void netlink_cycle_transport() {
  s_transport = (TransportMode)((s_transport + 1) % 3);
  s_prefs.putUChar("transport", s_transport);
  netlink_force_reconnect();
}

void netlink_cycle_netpref() {
  if (s_pi_ssid[0]) {
    s_netpref = (NetPref)((s_netpref + 1) % 3);
  } else {
    s_netpref = (NetPref)((s_netpref + 1) % 2);
  }
  s_prefs.putUChar("netpref", s_netpref);
  wifi_apply_pref();
}

const char *netlink_transport_label() {
  switch (s_transport) {
    case TRANS_USB:  return "USB";
    case TRANS_WIFI: return "WIFI";
    default:         return "AUTO";
  }
}

const char *netlink_netpref_label() {
  switch (s_netpref) {
    case NET_SECONDARY: return "HOTSPOT";
    case NET_FROM_PI:   return "FROM PI";
    default:            return "HOME";
  }
}

bool netlink_wifi_up() {
  return WiFi.status() == WL_CONNECTED;
}

bool netlink_link_up() {
  return (millis() - s_last_state_ms) < 2000;
}

bool netlink_usb_active() {
  return s_usb_seen && usb_want();
}

const char *netlink_ruby_ip() {
  if (!s_have_ruby_ip) return "--";
  static char buf[20];
  strlcpy(buf, s_ruby_ip.toString().c_str(), sizeof(buf));
  return buf;
}

int netlink_rssi() {
  return netlink_wifi_up() ? WiFi.RSSI() : 0;
}

void netlink_request_wifi_sync() {
  netlink_send_cmd("wifi_sync", 0, 0);
}
