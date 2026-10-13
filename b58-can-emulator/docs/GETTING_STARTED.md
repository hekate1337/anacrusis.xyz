# Getting started

Three steps: create a virtual CAN interface, start the emulator on it, point a diagnostic tool at it.
You need Linux with a real kernel (a VM or bare metal; Docker works only with `--network host` and the
`vcan` module loaded on the host; WSL2 needs a custom kernel with `CONFIG_CAN_VCAN`; macOS/Windows have
no SocketCAN). Python ≥ 3.9, no extra packages for the emulator itself.

## 1. Set up vcan

```bash
cd b58-can-emulator
sudo scripts/setup_vcan.sh vcan0
```

This loads the `vcan` kernel module and creates and brings up `vcan0`. Check with `ip -details link show vcan0`.
If `modprobe vcan` fails on Ubuntu, install the extra modules first:
`sudo apt install linux-modules-extra-$(uname -r)`.
The interface is gone after a reboot; run the script again.

## 2. Start the emulator

```bash
python3 -m bmw_b58_emu -i vcan0 --start-engine
```

It prints `B58 DME emulator on vcan0: BMW diag 0x6F1->0x612 (ECU 0x12), OBD-II 0x7DF/0x7E0->0x7E8, API http://127.0.0.1:8080/`
and keeps running until Ctrl+C. Useful options:

| Option | Effect |
| --- | --- |
| `--scenario idle\|cruise\|wot\|cycle` (`--speed 100`) | what the virtual driver does |
| `--thermal-scale 20` | warm up 20× faster |
| `--state-file state.json` | keep DTCs, VIN, coding and odometer across restarts |
| `--vin WBA…` | use your own VIN |
| `--no-broadcast` | diagnostics only, no cyclic PT-CAN frames |
| `-v` / `-vv` | log info / every CAN frame |

Or install it (`pip install .`) and use `b58-emu` / `b58-tester`.

Change the car while your tool is connected, through the HTTP API:

```bash
curl -s localhost:8080/api/state | python3 -m json.tool          # all signals
curl -s -XPOST localhost:8080/api/driver -d '{"mode":"cruise","target_speed":130}'
curl -s -XPOST localhost:8080/api/dtcs   -d '{"code":"P0301"}'   # inject a fault (MIL on, freeze frame)
curl -s -XDELETE localhost:8080/api/dtcs                         # clear all faults
curl -s -XPOST localhost:8080/api/override -d '{"signal":"coolant","value":115}'
curl -s -XPOST localhost:8080/api/ignition -d '{"state":"off"}'
```

## 3. Connect a diagnostic tool

| Talking to | Request ID | Response ID | Addressing |
| --- | --- | --- | --- |
| OBD-II (SAE J1979) | `0x7DF` functional / `0x7E0` physical | `0x7E8` | normal 11-bit ISO-TP |
| BMW DME, UDS (ISO 14229) | `0x6F1`, first byte `0x12` (`0xDF` functional) | `0x612`, first byte `0xF1` | extended ISO-TP |

**Quick check, built in:**

```bash
python3 -m bmw_b58_emu.tester -i vcan0 obd 01 0C       # -> 41 0C 0F A0  (rpm × 4)
python3 -m bmw_b58_emu.tester -i vcan0 obd 09 02       # -> 49 02 01 + VIN
python3 -m bmw_b58_emu.tester -i vcan0 bmw 22 F1 90    # -> 62 F1 90 + VIN
python3 -m bmw_b58_emu.tester -i vcan0 bmw 19 02 FF    # -> 59 02 … stored DTCs
```

**Python apps (python-can + can-isotp + udsoncan)**: [`examples/udsoncan_client.py`](../examples/udsoncan_client.py)
shows the connection setup for both OBD-II and BMW UDS and is run in CI.

```bash
pip install python-can can-isotp udsoncan
python3 examples/udsoncan_client.py vcan0
```

**can-utils** (`sudo apt install can-utils`; ISO-TP tools need the `can-isotp` kernel module, mainline since Linux 5.10):

```bash
candump vcan0                                                  # watch everything
isotprecv -s 7E0 -d 7E8 vcan0 &                                # print OBD responses
echo "01 0C" | isotpsend -s 7E0 -d 7E8 vcan0
isotprecv -s 6F1 -d 612 -x 12:F1 vcan0 &                       # print BMW responses
echo "22 F1 90" | isotpsend -s 6F1 -d 612 -x 12:F1 vcan0
```

**GUI tools** such as SavvyCAN or cantools-based viewers: open the `socketcan` device `vcan0` and load
[`b58_ptcan.dbc`](b58_ptcan.dbc) to decode the broadcast frames.

**ELM327 apps and BMW ENET tools** (most phone OBD apps, ISTA, E-Sys) do not speak SocketCAN. They need an
ELM327-serial or HSFZ-over-TCP bridge in front of `vcan0`, which this project does not include.

## Troubleshooting

* `cannot open CAN interface 'vcan0'`: the interface doesn't exist or is down. Run step 1 again.
* Tool gets no answer: check `candump vcan0` shows your request. Ignition off, an ECU reset or
  `28 01 01` (CommunicationControl) make the DME go quiet on purpose.
* `Operation not permitted` from `modprobe`/`ip`: these need root (`sudo`).

Protocol details (all DIDs, PIDs, routines, broadcast layout): [PROTOCOL.md](PROTOCOL.md).
