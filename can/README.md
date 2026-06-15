# MX-5 ND CAN reference

`MX5ND_HSCAN.dbc` — HS-CAN (500 kbit/s) database for the 2017 MX-5 ND, from the
community project berumiya/CAN_DBC_6thGenMazda (also see mx5things.blog and
Sebastian Schmoll's signal spreadsheet).

Signals wired into `hud/rubyhud/signals.py` `_decode_mx5()` (all big-endian):
| Signal | ID | Bytes | Formula |
|---|---|---|---|
| Engine RPM | 0x202 | 0-1 | raw * 0.25 |
| Vehicle speed | 0x202 | 2-3 | raw * 0.01 km/h (* 0.621371 -> mph) |
| Throttle/accel % | 0x202 | 4-5 | raw * 0.0015625 |
| Coolant degC | 0x420 | 0 | raw - 40 |
| Fuel | 0x9E | 5 | raw * 0.2 L (ND tank ~45 L -> %) |
| Gear (MT actual) | 0xFD | bit 19, len 3 | on-car reverse notes exist (signals.py); full shift-test verification still TODO |
| Wheel speeds | 0x215 (+ABS) | 0-1.. | raw * 0.01 - 100 km/h |
| Roof status | 0x472 | byte1/byte2 | CALIBRATED on-car (closed 00 05 00, open 0C 03 00; byte2 hi-nibble 2=opening 4=closing, +8=blink) |

NOT on HS-CAN: oil temperature, system/battery voltage (only a low-key-fob battery warning bit; HUD shows battery voltage from other sources or `--` in live mode while sim supplies values), intake/manifold temp (PI TEMP tile shows CPU SoC temp). The Sport-gauge oil temp is computed in the cluster.

Ambient temperature (0x420 b6-7) appears in the DBC and Snapshot.ambient_c but is intentionally not decoded on this ND1 in `signals.py` (see ND1 notes). Gear @0xFD has on-car notes but remains marked for fuller verification. Roof @0x472 is **calibrated** (see `signals._decode_mx5`): byte2 hi-nibble = motion (0 idle / 2 opening / 4 closing, +0x8 blink), byte1 when idle = state (0x05 closed / 0x03 open).
