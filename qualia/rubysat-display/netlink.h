// netlink.h — transport layer for Qualia <-> Ruby Pi (USB CDC or Wi-Fi TCP).
//
// Modes (persisted in NVS):
//   TRANS_AUTO  — USB if a STATE line arrives within USB_PROBE_MS, else Wi-Fi
//   TRANS_USB   — native USB CDC only (Serial)
//   TRANS_WIFI  — Wi-Fi TCP client only (mDNS ruby.local -> fallback IP)
//
// Wi-Fi network preference (persisted):
//   NET_PRIMARY    — secrets.h WIFI_SSID / WIFI_PASS
//   NET_SECONDARY  — secrets.h WIFI_SSSID2 / WIFI_PASS2
//   NET_FROM_PI    — credentials received from a Pi wifi_sync over USB
//
// The wire protocol is identical on both transports: newline-delimited JSON.
// STATE lines update gauges; {"type":"wifi",...} lines store synced creds.

#ifndef RUBYSAT_NETLINK_H
#define RUBYSAT_NETLINK_H

#include <Arduino.h>

enum TransportMode : uint8_t {
  TRANS_AUTO = 0,
  TRANS_USB  = 1,
  TRANS_WIFI = 2,
};

enum NetPref : uint8_t {
  NET_PRIMARY   = 0,
  NET_SECONDARY = 1,
  NET_FROM_PI   = 2,
};

void netlink_begin();
void netlink_pump();

void netlink_send_cmd(const char *cmd, int x, int y);
void netlink_force_reconnect();

TransportMode netlink_transport();
NetPref       netlink_netpref();
void          netlink_cycle_transport();
void          netlink_cycle_netpref();
const char   *netlink_transport_label();
const char   *netlink_netpref_label();

bool netlink_wifi_up();
bool netlink_link_up();       // fresh STATE within 2s
bool netlink_usb_active();    // USB transport carrying data now
const char *netlink_ruby_ip();
int         netlink_rssi();

void netlink_request_wifi_sync();

// Called by netlink when a full STATE JSON line arrives.
void netlink_on_state_line(const String &line);

#endif  // RUBYSAT_NETLINK_H
