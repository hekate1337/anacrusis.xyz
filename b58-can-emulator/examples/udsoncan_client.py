"""Talk to the emulator with the common Python diagnostic stack (python-can + can-isotp + udsoncan).

    pip install python-can can-isotp udsoncan
    python examples/udsoncan_client.py vcan0

This is how a diagnostic/maintenance app built on those libraries would connect; CI runs it against
the emulator to keep it working.
"""
import sys

import can
import isotp
import udsoncan
from udsoncan.client import Client
from udsoncan.connections import PythonIsoTpConnection

iface = sys.argv[1] if len(sys.argv) > 1 else "vcan0"
bus = can.Bus(interface="socketcan", channel=iface)

# OBD-II: physical requests 0x7E0 -> responses 0x7E8, normal 11-bit addressing.
obd = PythonIsoTpConnection(isotp.CanStack(bus, address=isotp.Address(
    isotp.AddressingMode.Normal_11bits, txid=0x7E0, rxid=0x7E8)))
obd.open()
obd.send(b"\x01\x0C")                                  # mode 01 PID 0C: engine speed
r = obd.wait_frame(timeout=2)
print(f"OBD  01 0C -> {r.hex(' ')}  ({int.from_bytes(r[2:4], 'big') / 4:.0f} rpm)")
obd.send(b"\x09\x02")                                  # mode 09 PID 02: VIN (multi-frame)
r = obd.wait_frame(timeout=2)
print(f"OBD  09 02 -> VIN {r[3:].decode()}")
obd.close()

# BMW UDS: 0x6F1 -> 0x612, extended addressing (target 0x12 = DME, source 0xF1 = tester).
bmw = PythonIsoTpConnection(isotp.CanStack(bus, address=isotp.Address(
    isotp.AddressingMode.Extended_11bits, txid=0x6F1, rxid=0x612, target_address=0x12, source_address=0xF1)))
config = dict(udsoncan.configs.default_client_config)
config["data_identifiers"] = {0xF190: udsoncan.AsciiCodec(17), 0xF189: udsoncan.AsciiCodec(16)}
with Client(bmw, config=config) as client:
    client.change_session(3)                           # extended diagnostic session
    print(f"UDS  22 F1 90 -> VIN {client.read_data_by_identifier(0xF190).service_data.values[0xF190]}")
    print(f"UDS  22 F1 89 -> SW  {client.read_data_by_identifier(0xF189).service_data.values[0xF189].rstrip(chr(0) + " ")}")
    dtcs = client.get_dtc_by_status_mask(0xFF).service_data.dtcs
    print(f"UDS  19 02 FF -> {len(dtcs)} DTC(s)")
bus.shutdown()
