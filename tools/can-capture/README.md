# Ruby — roof/window CAN+LIN capture kit

On-car reverse-engineering tools for the MX-5 ND1 RF roof + windows. **Everything
here is listen-only.** Nothing in this kit transmits; actuation is a separate,
later, explicitly-authorized step. See the `mx5-roof-window-can` research notes.

## The model (corrected — read this first)
The roof is **not** moved by injecting a magic CAN command (no such frame exists).
The proven mods (SmartTOP, mx5things RFC) work two ways at once:
1. **Switch emulation** — the factory roof switch is a simple **two-voltage-level**
   signal you **hold for the whole ~13 s cycle**; the mod drives those two voltages.
2. **Interlock filtering** — a CAN node that **suppresses only the speed/reverse
   "inhibit" frame** so the roof moves above the factory ~6 mph limit.

So the capture goals are: (a) measure the **two switch voltage levels** and their
behavior, (b) identify the **interlock frame ID + the value that means "inhibit"**,
(c) the roof-segment **bitrate**, (d) trustworthy **speed + trunk-closed** signals,
(e) the **window switch pinouts**. There is no command ID to hunt.

## Hard safety rules
- Every CAN interface comes up **listen-only**, verified (`setup-buses.sh` refuses
  otherwise). `ip -details link show` must show `LISTEN-ONLY`.
- **Back-probe or inline pigtail only** — never insulation-pierce the CAN twisted
  pair, never add a third 120 Ω terminator.
- **12 V on a Pi UART destroys the Pi** — the window LIN sniff goes through a LIN
  transceiver + 3.3 V level protection, metered before power-on.
- Before any roof/window cycle: **ignition on, P/N + parking brake, 0 mph, trunk
  CLOSED, body clear of the swing/pinch path, kill-switch in hand.** The RF stows
  *into* the trunk — an open trunk jams it. Don't over-cycle the roof motor.
- Battery maintainer on for long ignition-on sessions (brown-out corrupts frames).

## Phase 0 — this week, on the bench (no car)
Stand up a **second listen-only interface** beside `can0` and prove dual capture:
```
sudo ./setup-buses.sh can0 500000      # dash bus (the 0x472 clock)
sudo ./setup-buses.sh can1 500000      # 2nd interface, any bus for now
candump can0 &  candump can1           # both should stream; Ctrl-C
```
Success: both stream clean frames; `can0` still shows `0x472`/`0x274`; the
kill-switch drops the rig off the bus instantly. Order a back-probe kit + a LIN
transceiver. **Do not buy the Teensy/bridge/relays yet** — their config is unknown
until Phase 1.

## Phase 1 — on the car (key on, engine off)
**Roof (at the convertible-top controller's white+blue plugs under the side trim,
NOT OBD):**
```
sudo ./find-bitrate.sh can1                 # likely 125k (MS-CAN); confirm
sudo ./setup-buses.sh can1 <good-rate>
./capture.sh baseline   can0 can1           # ~60s untouched, Ctrl-C
./capture.sh roof-open  can0 can1           # while HOLDING the OEM switch open
./capture.sh roof-close can0 can1           # while HOLDING it close (several reps)
./diff-frames.py logs/baseline-can1-*.log logs/roof-open-can1-*.log \
                 --dash logs/roof-open-can0-*.log
```
Separately, in a listen-only run, watch which candidate **interlock** frame
changes value as you roll the car / select reverse — that's the one the bridge
must suppress. **Meter the roof switch signal line: record its two voltage levels**
(idle vs held-open vs held-close).

**Windows (at each window's own switch, low-current logic contacts — never motor
leads):** meter the driver master switch (terminals `1D`=up, `1F`=down, `1A`=gnd,
`1B`=+12 V, `1L`=serial-to-passenger) key-on, un-actuated vs each action, to map
sense pins + polarity. For the passenger side (serial/LIN), optionally:
```
./lin-capture.py /dev/serial0 19200 > logs/lin-baseline.txt
./lin-capture.py /dev/serial0 19200 > logs/lin-pass-up.txt   # diff byte2
```

## The dataset to come home with
1. Roof switch **two voltage levels** (idle / open / close).
2. The **interlock frame ID + inhibit value** to suppress, and the roof-segment
   **bitrate**.
3. Whether the interlock is suppressible by a **single node on a shared bus** vs a
   true **2-channel cut** (decides Teensy bridge vs simpler filter).
4. The roof controller **heartbeat/handshake** to preserve, and the **anti-trap /
   obstruction** frame(s) that must **never** be filtered.
5. Trustworthy **speed (0x202)**, **trunk-closed**, gear/reverse signals confirmed
   fresh on `can0` (the HUD already decodes these).
6. Window switch **pin map** (driver `1D`/`1F`; passenger subswitch) + polarity.

Then we pick hardware and wire the HUD's `Actuator` (already built + tested in sim:
`hud/rubyhud/safety.py`, `actuator.py`, `test_safety.py`).
