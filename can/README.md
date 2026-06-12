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
| Ambient degC | 0x420 | 6-7 | raw * 0.25 - 3200 |
| Fuel | 0x9E | 5 | raw * 0.2 L (ND tank ~45 L -> %) |
| Gear (MT actual) | 0xFD | bit 19, len 3 | TODO: verify with shift test |
| Wheel speeds | 0x215 (+ABS) | 0-1.. | raw * 0.01 - 100 km/h |
| Roof status | 0x472 | - | TODO: verify open/close |

NOT on HS-CAN: oil temperature, system/battery voltage (only a low-key-fob-
battery warning bit). The Sport-gauge oil temp is computed in the cluster.
