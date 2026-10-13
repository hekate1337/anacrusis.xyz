# Protocol reference

## Addressing

| Path | Tester → ECU | ECU → Tester | Notes |
|---|---|---|---|
| BMW D-CAN style (physical) | `0x6F1`, data[0] = `0x12` (DME) | `0x612`, data[0] = `0xF1` (tester) | ISO-TP **extended addressing**; 11-bit IDs |
| BMW functional | `0x6F1`, data[0] = `0xDF` | `0x612` | Negative responses 0x11/0x12/0x31/0x7E/0x7F are suppressed |
| OBD-II physical | `0x7E0` | `0x7E8` | Normal addressing, pad `0x55`. Also accepts UDS (SID ≥ 0x10) |
| OBD-II functional | `0x7DF` | `0x7E8` | Single frames only |

Classic CAN (8-byte frames), 500 kbit/s is the conventional rate (irrelevant on `vcan`).
Example: reading the VIN with `cansend`:

```
cansend vcan0 6F1#120322F190      # 12 = DME, 03 = ISO-TP SF len 3, 22 F1 90 = ReadDataByIdentifier VIN
candump vcan0                     # 612#F11014 62F190 57...  (first frame), then flow control / consecutive frames
```

With `can-utils` ISO-TP (kernel `can-isotp` module): `echo "22 F1 90" | isotpsend -s 6F1 -d 612 -x 12:F1 vcan0`
and `isotpsend -s 7E0 -d 7E8 vcan0` for OBD.

## OBD-II (SAE J1979)

| Mode | Support |
|---|---|
| 01 | Live data; up to 6 PIDs per request. Supported-PID bitmaps (00/20/40/60/80/A0/C0) are generated from the implemented set |
| 02 | Freeze frame of the first stored fault (PIDs 02, 04, 05, 0B, 0C, 0D, 0F, 10, 11) |
| 03 / 07 | Confirmed / pending DTCs (`43 <count> <DTC…>`, CAN form) |
| 04 | Clear DTCs and reset "since clear" counters, MIL off |
| 09 | PID 00, 02 (VIN), 04 (calibration ID), 06 (CVN), 0A (ECU name) |
| 0A | Always empty (permanent DTCs not modelled) |

Unsupported PIDs get no response (as most ECUs do); unsupported modes on a physical request get NRC `0x11`.
Mode 01 PIDs: 01 03 04 05 06 07 0B 0C 0D 0E 0F 10 11 13 1C 1F 21 23 24 2F 30 31 33 3C 42 43 44 45 46 49 4A 4C 4D 4E 51 5C 5E 62 63 A6.

## UDS (ISO 14229-1)

| SID | Service | Details |
|---|---|---|
| 10 | DiagnosticSessionControl | 01 default, 02 programming, 03 extended. P2 = 50 ms, P2* = 5000 ms. S3 timeout 5 s drops back to default (re-locks security, re-enables DTC logging and cyclic TX) |
| 11 | ECUReset | 01/02/03. Positive response, then the DME goes silent ~0.8 s and resets session state |
| 14 | ClearDiagnosticInformation | `FFFFFF` = all, or a specific DTC |
| 19 | ReadDTCInformation | 01 (count), 02 (by mask), 04 (snapshot), 06 (extended data), 0A (supported) |
| 22 | ReadDataByIdentifier | Multiple DIDs per request, multi-frame responses |
| 27 | SecurityAccess | Levels `01 03 05 11` (seed odd / key even). 3 bad keys → NRC 0x36 then 10 s lockout (0x37). Requires a non-default session |
| 28 | CommunicationControl | `00` enable, `01` disable TX, `02` disable RX, `03` disable both. Silences the cyclic frames |
| 2E | WriteDataByIdentifier | Needs non-default session + security level ≥ 1 for `F190` (VIN) and `C000` (16-byte coding block) |
| 31 | RoutineControl | `F001` reset fuel-trim adaptation, `F003` self-test, `FF00` simulated erase (programming session + security) |
| 3E | TesterPresent | `3E 00`, `3E 80` suppresses the response |
| 85 | ControlDTCSetting | `01` on, `02` off (faults are not logged while off) |

Not implemented: 0x23/0x3D memory access, 0x34/0x36/0x37 download/transfer (flashing), 0x2F IO control, NRC 0x78 pending.

**Security algorithm (emulator-defined, not BMW's):** `key = (rotl32(seed ^ 0xB58B58B5, 5) + level * 0x0101) mod 2^32`
(`bmw_b58_emu.uds.compute_key`).

### Identification DIDs
`F190` VIN, `F186` active session, `F187` part number, `F189` SW version, `F18A` supplier, `F18B` manufacturing date,
`F18C` serial, `F191` HW version, `F197` system name, `C000` coding block (emulator).

### Live-data DIDs (emulator-defined; big-endian, `value = raw * scale + offset`)

| DID | Signal | Type | Scale | Offset | Unit |
|---|---|---|---|---|---|
| `0xD001` | Engine speed | `>H` (2 B) | 1.0 | 0 | rpm |
| `0xD002` | Vehicle speed | `>H` (2 B) | 0.01 | 0 | km/h |
| `0xD003` | Coolant temperature | `>h` (2 B) | 0.1 | 0 | degC |
| `0xD004` | Engine oil temperature | `>h` (2 B) | 0.1 | 0 | degC |
| `0xD005` | Intake air temperature | `>h` (2 B) | 0.1 | 0 | degC |
| `0xD006` | Manifold absolute pressure | `>H` (2 B) | 0.1 | 0 | kPa |
| `0xD007` | Ambient pressure | `>H` (2 B) | 0.1 | 0 | kPa |
| `0xD008` | Accelerator pedal | `>H` (2 B) | 0.01 | 0 | % |
| `0xD009` | Throttle plate angle | `>H` (2 B) | 0.01 | 0 | % |
| `0xD00A` | Engine torque (indicated) | `>h` (2 B) | 0.1 | 0 | Nm |
| `0xD00B` | Ignition timing | `>h` (2 B) | 0.1 | 0 | deg BTDC |
| `0xD00C` | Rail pressure (high) | `>H` (2 B) | 1.0 | 0 | kPa |
| `0xD00D` | Lambda (measured) | `>H` (2 B) | 0.001 | 0 | - |
| `0xD00E` | Battery voltage | `>H` (2 B) | 0.001 | 0 | V |
| `0xD00F` | Fuel level | `>H` (2 B) | 0.1 | 0 | % |
| `0xD010` | Air mass flow | `>H` (2 B) | 0.1 | 0 | g/s |
| `0xD011` | Odometer | `>I` (4 B) | 0.1 | 0 | km |
| `0xD012` | Engine run time | `>I` (4 B) | 1.0 | 0 | s |
| `0xD013` | Gear | `>B` (1 B) | 1.0 | 0 | - |
| `0xD014` | Terminal status (0=KL30,1=KL R,2=KL15) | `>B` (1 B) | 1.0 | 0 | - |
| `0xD015` | Engine state (0=off,1=cranking,2=running) | `>B` (1 B) | 1.0 | 0 | - |
| `0xD016` | Boost pressure (relative) | `>H` (2 B) | 0.1 | 0 | kPa |

## Cyclic broadcast frames

Sent while the ignition is on (bus "sleeps" with KL15/KL R off) and while CommunicationControl allows it.
Byte 0 = CRC-8 (SAE J1850: poly 0x1D, init 0xFF, xor-out 0xFF) over bytes 1‥7; byte 1 = `0xF0 | alive counter`.
Machine-readable layout: [`b58_ptcan.dbc`](b58_ptcan.dbc) (load in SavvyCAN, cantools, BusMaster, …).

| ID | Period | Content |
|---|---|---|
| `0x0A5` | 20 ms | torque, rpm, pedal, engine running |
| `0x1A1` | 20 ms | vehicle speed, brake, gear |
| `0x1D0` | 100 ms | coolant / oil / intake temps, boost, ambient pressure |
| `0x12F` | 100 ms | terminal status (KL R/15/50), MIL, engine state, battery |
| `0x2CA` | 1 s | ambient temperature |
| `0x330` | 1 s | odometer, fuel level |
| `0x512` | 1 s | network-management heartbeat (`0x500 + address`) |

## Control API (HTTP, default `127.0.0.1:8080`)

| Request | Body / query | Effect |
|---|---|---|
| `GET /api/state` | | All signals, driver mode, overrides, DME/UDS state, frame counters |
| `POST /api/ignition` | `{"state":"off\|accessory\|on"}` | Terminal state |
| `POST /api/engine` | `{"action":"start\|stop"}` | Crank / stall |
| `POST /api/driver` | `{"mode":"idle\|manual\|cruise\|wot\|cycle","target_speed":100,"pedal":40,"brake":0}` | Virtual driver; `manual` uses pedal/brake directly |
| `GET /api/dtcs` | | Stored faults |
| `POST /api/dtcs` | `{"code":"P0301","confirmed":true}` | Inject a fault (captures a freeze frame, lights MIL if confirmed). `P030x` also makes the engine run rough |
| `DELETE /api/dtcs` | `?code=P0301` (optional) | Clear one / all |
| `POST /api/override` | `{"signal":"coolant","value":120}` | Pin any signal (fault/limit testing) |
| `DELETE /api/override` | `?signal=coolant` (optional) | Release one / all |

The API is unauthenticated: keep it on loopback unless you know what you are doing.
