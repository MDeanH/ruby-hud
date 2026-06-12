"""SpcReader -- read AC-present + battery state from a SunFounder PiPower 5.

The PiPower 5 UPS HAT is a SunFounder "SPC" (System Power Controller) device: a
Cortex-M23 MCU that sits on the Raspberry Pi's I2C-1 bus (GPIO2/GPIO3) at
address 0x5C and exposes a flat register map. We do NOT depend on SunFounder's
`spc`/`pipower5` packages (they are not installed in ruby-env and pull in extra
deps); instead we talk to the firmware directly with smbus2, which IS already in
ruby-env. The register layout below is transcribed verbatim from the upstream
driver (github.com/sunfounder/spc, spc/spc.py, device 0x5C "mode: normal") so
the wire protocol matches the firmware exactly:

  * "normal" mode == plain SMBus: read_byte_data / read_i2c_block_data with the
    register as the SMBus command byte.
  * 16-bit values are LITTLE-ENDIAN (low byte at reg, high byte at reg+1),
    matching the upstream `_unpack_u16(data, reg) = data[reg+1]<<8 | data[reg]`.

Register map (subset we use), all reads start the common block at reg 0:

  reg  8  battery_voltage      u16 LE, millivolts
  reg 12  battery_percentage   u8,    percent (0..100)
  reg 15  power_source         u8,    0 = external (AC), 1 = battery
  reg 16  is_input_plugged_in  u8,    0 = AC unplugged, 1 = AC plugged in
  reg 18  is_charging          u8,    0/1
  reg 20  shutdown_request     u8,    0 none / 1 low-battery / 2 button / 3 low-V

AC-present is read from TWO independent firmware signals so a single flaky one
can't fool us:
  * is_input_plugged_in == 1  -> the barrel/USB-C input has voltage, AND
  * power_source == 0 (EXTERNAL) -> the HAT is feeding the Pi from input, not
    draining the cells.
We treat AC as PRESENT only when the input is plugged in and the HAT is NOT
running on battery. That is the conservative choice for a shutdown trigger: we
declare "power lost" only when BOTH say so (see ac_present()).

Hardware deps (smbus2) are imported lazily INSIDE methods so this module
imports fine on a bare host with no I2C; open() raises SpcUnavailable on any
failure and the daemon treats the HAT as absent (reports unknown, never trips
the shutdown).
"""

from __future__ import annotations

# PiPower 5 SPC I2C address + bus (verified: SunFounder docs + spc/devices.py).
SPC_ADDR = 0x5C
SPC_BUS = 1

# Common read block: reg 0, 25 bytes (REG_READ_COMMON_LENGTH upstream).
_BLK_START = 0
_BLK_LEN = 25

# Register offsets within the common block (spc/spc.py, verbatim).
_R_BATTERY_VOLTAGE = 8       # u16 LE, mV
_R_BATTERY_PERCENTAGE = 12   # u8, percent
_R_POWER_SOURCE = 15         # u8, 0 external / 1 battery
_R_IS_INPUT_PLUGGED_IN = 16  # u8, 0/1
_R_IS_CHARGING = 18          # u8, 0/1
_R_SHUTDOWN_REQUEST = 20     # u8, 0..3

# Firmware version block (sanity check the device is really an SPC).
_R_FW_MAJOR = 128

POWER_SOURCE_EXTERNAL = 0
POWER_SOURCE_BATTERY = 1

SHUTDOWN_REQUEST_NONE = 0
SHUTDOWN_REQUEST_LOW_BATTERY = 1
SHUTDOWN_REQUEST_BUTTON = 2
SHUTDOWN_REQUEST_LOW_VOLTAGE = 3


class SpcUnavailable(Exception):
    """Raised when the PiPower 5 cannot be reached (no bus / no device / IO)."""


class SpcReading:
    """One immutable snapshot of the HAT state. All fields may be None if a
    read partially failed; ac_present() degrades safely in that case."""

    __slots__ = ("ac_input", "on_battery", "battery_pct", "battery_mv",
                 "charging", "shutdown_request", "ok")

    def __init__(self, ac_input=None, on_battery=None, battery_pct=None,
                 battery_mv=None, charging=None, shutdown_request=None,
                 ok=False):
        self.ac_input = ac_input            # bool: input plugged in
        self.on_battery = on_battery        # bool: power_source == BATTERY
        self.battery_pct = battery_pct      # int 0..100 or None
        self.battery_mv = battery_mv        # int millivolts or None
        self.charging = charging            # bool or None
        self.shutdown_request = shutdown_request  # int 0..3 or None
        self.ok = ok                        # bool: this read fully succeeded

    def ac_present(self):
        """True when AC/external power is present, False when on battery, None
        when genuinely unknown.

        Conservative for a shutdown trigger: returns False (power lost) ONLY
        when the firmware affirmatively says the input is unplugged OR that it
        is now sourcing from the battery. If we could not read the HAT at all we
        return None so the daemon does NOT count it as a power-loss tick."""
        if not self.ok or (self.ac_input is None and self.on_battery is None):
            return None
        # AC is present when the input is plugged in AND we are not on battery.
        plugged = bool(self.ac_input)
        on_bat = bool(self.on_battery)
        return plugged and not on_bat

    def as_dict(self):
        return {
            "ok": bool(self.ok),
            "ac_present": self.ac_present(),
            "ac_input": self.ac_input,
            "on_battery": self.on_battery,
            "battery_pct": self.battery_pct,
            "battery_mv": self.battery_mv,
            "charging": self.charging,
            "shutdown_request": self.shutdown_request,
        }


class SpcReader:
    """Thin smbus2 client for the PiPower 5 at 0x5C on I2C-1.

    Open is lazy and guarded: construct freely on any host; call open() to bind
    the bus (raises SpcUnavailable if smbus2 / the bus / the device is missing).
    read() never raises -- it returns an SpcReading with ok=False on any IO
    error so the monitor loop is fully decoupled from a flaky bus."""

    def __init__(self, addr: int = SPC_ADDR, bus: int = SPC_BUS):
        self.addr = int(addr)
        self.busnum = int(bus)
        self._bus = None
        self.firmware = None

    def open(self) -> None:
        """Bind the SMBus and confirm the SPC answers. Raises SpcUnavailable."""
        try:
            from smbus2 import SMBus  # lazy: bare hosts have no smbus2
        except Exception as exc:
            raise SpcUnavailable("smbus2 import failed: %s" % exc)
        try:
            bus = SMBus(self.busnum)
        except Exception as exc:
            raise SpcUnavailable(
                "cannot open /dev/i2c-%d: %s" % (self.busnum, exc))
        # Probe: read the firmware version block. A successful 3-byte read at
        # 0x5C is strong evidence this really is the SPC (and the I2C bus is up
        # and the HAT is powered). A bare bus with nothing at 0x5C raises OSError
        # here -> SpcUnavailable, which the daemon reports without ever tripping.
        try:
            fw = bus.read_i2c_block_data(self.addr, _R_FW_MAJOR, 3)
            self.firmware = "%d.%d.%d" % (fw[0], fw[1], fw[2])
        except Exception as exc:
            try:
                bus.close()
            except Exception:
                pass
            raise SpcUnavailable(
                "no SPC at 0x%02X on i2c-%d: %s"
                % (self.addr, self.busnum, exc))
        self._bus = bus

    def read(self) -> SpcReading:
        """Read one snapshot. NEVER raises: returns ok=False on any error."""
        bus = self._bus
        if bus is None:
            return SpcReading(ok=False)
        try:
            blk = bus.read_i2c_block_data(self.addr, _BLK_START, _BLK_LEN)
        except Exception:
            return SpcReading(ok=False)
        try:
            mv = blk[_R_BATTERY_VOLTAGE] | (blk[_R_BATTERY_VOLTAGE + 1] << 8)
            pct = blk[_R_BATTERY_PERCENTAGE]
            src = blk[_R_POWER_SOURCE]
            plugged = blk[_R_IS_INPUT_PLUGGED_IN]
            charging = blk[_R_IS_CHARGING]
            shutdown = blk[_R_SHUTDOWN_REQUEST]
        except (IndexError, TypeError):
            return SpcReading(ok=False)
        # Coerce/bound defensively (firmware is trusted, but never crash).
        pct = pct if 0 <= pct <= 100 else None
        return SpcReading(
            ac_input=(plugged == 1),
            on_battery=(src == POWER_SOURCE_BATTERY),
            battery_pct=pct,
            battery_mv=int(mv) if mv else (0 if mv == 0 else None),
            charging=(charging == 1),
            shutdown_request=int(shutdown),
            ok=True,
        )

    def close(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
