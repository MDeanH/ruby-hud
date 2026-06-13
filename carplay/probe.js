// CarlinKit dongle probe — detects the CarPlay dongle via libusb, matched
// against node-carplay's authoritative knownDevices list. Safe to run any time;
// exits 0 if a dongle is present, 2 if not. No connection/handshake attempted.
import { getDeviceList } from "usb"
import { DongleDriver } from "node-carplay/node"

const known = DongleDriver.knownDevices
const hex = (n) => "0x" + n.toString(16).padStart(4, "0")
const devs = getDeviceList()
const match = devs.find((d) =>
  known.some((k) =>
    k.vendorId === d.deviceDescriptor.idVendor &&
    k.productId === d.deviceDescriptor.idProduct))

console.log("known CarlinKit IDs:",
  known.map((k) => hex(k.vendorId) + ":" + hex(k.productId)).join(", "))
if (match) {
  const dd = match.deviceDescriptor
  console.log(`DONGLE FOUND: ${hex(dd.idVendor)}:${hex(dd.idProduct)} ` +
    `(bus ${match.busNumber} addr ${match.deviceAddress})`)
  process.exit(0)
} else {
  console.log("NO DONGLE. USB devices present:",
    devs.map((d) => hex(d.deviceDescriptor.idVendor) + ":" +
      hex(d.deviceDescriptor.idProduct)).join(", "))
  process.exit(2)
}
