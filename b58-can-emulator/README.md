# BMW B58 CAN bus emulator

A Python backend that emulates the CAN side of a BMW B58 (3.0 L turbo I6) car on a **virtual SocketCAN
interface (`vcan0`)**: a DME with BMW-style UDS diagnostics, a standard OBD-II ECU, and the cyclic
powertrain broadcast traffic — all backed by a small engine/gearbox/vehicle simulation, so diagnostic
and maintenance tools see coherent, changing data. No third-party dependencies (Python ≥ 3.9, Linux for SocketCAN).

```
 maintenance tool / scanner ──► vcan0 ◄── bmw_b58_emu ──► vehicle model (rpm, boost, temps, fuel, DTCs)
 (candump, SavvyCAN, python-can,           │                  ▲
  udsoncan, isotpsend, …)                  └── HTTP API ──────┘  (drive it, inject faults)
```

## Quick start

```bash
cd b58-can-emulator
sudo scripts/setup_vcan.sh vcan0          # modprobe vcan + create/up the interface (needs a real Linux kernel)
python3 -m bmw_b58_emu -i vcan0 --start-engine --scenario cruise --speed 100
```
(`--create-vcan` does the setup itself when run as root.) In another terminal:

```bash
candump vcan0                                              # live PT-CAN traffic
python3 -m bmw_b58_emu.tester -i vcan0 obd 01 0C           # engine speed  -> 41 0C xx xx
python3 -m bmw_b58_emu.tester -i vcan0 obd 09 02           # VIN
python3 -m bmw_b58_emu.tester -i vcan0 bmw 22 F1 90        # DME VIN via 0x6F1 -> 0x612
curl -s localhost:8080/api/state | python3 -m json.tool
curl -s -XPOST localhost:8080/api/dtcs -d '{"code":"P0301"}'   # inject a misfire; MIL on, freeze frame stored
curl -s -XPOST localhost:8080/api/driver -d '{"mode":"wot"}'   # floor it
```

What's on the bus — full tables in [docs/PROTOCOL.md](docs/PROTOCOL.md):

* **BMW diagnostics**: `0x6F1`→`0x612` (ECU `0x12`, tester `0xF1`, extended addressing; functional `0xDF`). UDS sessions,
  ECU reset, DTC read/clear/snapshot, ReadDataByIdentifier (ID + live data), WriteDataByIdentifier, SecurityAccess with
  lockout, routines, CommunicationControl, ControlDTCSetting, S3 timeout. Full ISO-TP incl. flow control.
* **OBD-II**: `0x7DF`/`0x7E0`→`0x7E8`; modes 01, 02, 03, 04, 07, 09 (VIN, cal ID, CVN), 0A; ~40 PIDs with correct scaling.
* **Broadcast**: engine, speed, temps, terminal status, ambient, odometer, NM heartbeat with CRC8 + alive counters,
  [DBC provided](docs/b58_ptcan.dbc). Goes quiet with ignition off or after `28 01 01`.
* **Simulation**: cranking, cold/warm idle, 8-speed auto, turbo boost, rev + 250 km/h limiters, thermals, fuel burn,
  drive cycle, scripted cruise/WOT, signal overrides, fault injection with freeze frames.

Options: `python3 -m bmw_b58_emu --help` (`--state-file` persists DTCs/VIN/coding/odometer, `--thermal-scale 20` warms up
20× faster, `--no-broadcast`, `-vv` traces every frame).

## Using it with other tools

* **can-utils**: `isotpsend -s 6F1 -d 612 -x 12:F1 vcan0` / `isotpsend -s 7E0 -d 7E8 vcan0` (needs the `can-isotp` module).
* **python-can / udsoncan / SavvyCAN / cantools**: point at `vcan0`; load `docs/b58_ptcan.dbc` for decoding.
* **In Python** without any of the above: `from bmw_b58_emu.tester import bmw_client, obd_client`.

Tools that talk **ELM327 serial/Wi-Fi** or **BMW ENET/HSFZ over TCP** (ISTA, E-Sys, most phone OBD apps) cannot attach
to SocketCAN directly; they need a bridge (e.g. an ELM327-to-SocketCAN shim) — not included.

## Fidelity — please read

This emulates the *behaviour and framing* of the protocols, not BMW's proprietary data. Specifically **not** real BMW
data: the broadcast signal layouts (IDs/style follow F/G-series PT-CAN conventions, contents are emulator-defined),
the live-data DIDs (`0xD0xx`), the SecurityAccess algorithm, DME part/HW/SW strings and the default VIN (fake, checksum
not valid). The standard parts — ISO-TP, UDS framing/NRCs, OBD-II PIDs/scaling, ISO identification DIDs — follow the
specs. Flash/programming services are intentionally absent. The physics is a plausibility model, not calibrated.

## Tests

```bash
python3 -m unittest discover -s tests -t .     # ~30 s; runs over an in-process virtual bus, no root needed
```
Covers ISO-TP (multi-frame, block size/STmin), OBD, UDS (sessions, security, lockout, writes, DTCs, resets), broadcast
CRC/counters, vehicle model, and the HTTP API. **The SocketCAN transport itself is not covered by the automated
tests** (no CAN support in the CI/sandbox kernel) — verify it once on your machine with the quick start above.

## Layout

`bus.py` SocketCAN + in-memory bus · `isotp.py` ISO 15765-2 · `node.py` RX demux + cyclic scheduler · `dme.py` ECU wiring ·
`uds.py` / `obd.py` / `dids.py` / `dtc.py` protocol logic · `vehicle.py` simulation · `broadcast.py` cyclic frames ·
`api.py` control API · `tester.py` client/CLI · `vcan.py` interface helper.
