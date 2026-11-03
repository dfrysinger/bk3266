# bk3266

An agent skill and a single-file Python CLI for **Beken BK3266** Bluetooth audio chips, the chip inside many cheap
headphones, sleep headbands, speakers and Bluetooth receivers. With it you can:

- **Back up** the whole SPI flash over a 3.3 V UART, with two passes that must match
- **Decode** the configuration: battery thresholds, prompt volume, timeouts, reconnect behaviour, sniff/power saving
- **Extract** the voice prompts as playable audio
- **Patch** settings offline (CRCs recomputed) and **write them back safely**: dry run by default, pre-image check, only changed sectors, verified readback, original write protection restored
- **Disassemble** the firmware (it's BA2, not ARM)
- **Diagnose Bluetooth drops** from an iPhone PacketLogger capture

It works with **Claude Code, Codex CLI and GitHub Copilot CLI**. The skill teaches the agent the protocol, the safety rules
and how to walk a non-specialist through pressing the button at the right moment.

> **Status:** reads, config decoding and a config write (muting the low-battery prompt) are verified on real hardware
> (512 KiB Puya flash, 2021 firmware). Other flash sizes or firmware builds may need `--config-base`. Read the field
> confidence notes before writing anything.

## Install

### Claude Code
```sh
claude plugin marketplace add dfrysinger/bk3266 --scope user
claude plugin install --scope user bk3266@bk3266
```

### Codex CLI
```sh
codex plugin marketplace add dfrysinger/bk3266
codex plugin add bk3266@bk3266
```

### GitHub Copilot CLI
```sh
copilot plugin install dfrysinger/bk3266
```

### Just the CLI
```sh
pip install pyserial
python3 skills/bk3266/scripts/bk3266.py -h
```

## Quick start

Wire a **3.3 V** USB-UART to the chip's TX/RX pads and ground, leaving VCC disconnected (see [hardware](skills/bk3266/references/hardware.md)). Then:

```sh
S=skills/bk3266/scripts/bk3266.py
python3 $S ping --port /dev/cu.usbserial-XXXX --say      # press and hold power when told
python3 $S dump --port /dev/cu.usbserial-XXXX --say --out backups
python3 $S config backups/dump-*/flash-dump-1.bin
python3 $S patch backups/dump-*/flash-dump-1.bin -o muted.bin --mute-prompt low-battery
python3 $S write muted.bin --expect backups/dump-*/flash-dump-1.bin --port /dev/cu.usbserial-XXXX        # dry run
python3 $S write muted.bin --expect backups/dump-*/flash-dump-1.bin --port /dev/cu.usbserial-XXXX --yes  # for real
```

## The key protocol discovery

The ROM bootloader answers LinkCheck `01 E0 FC 01 00` during power-on, but it **drops back to the application after
one read unless you also send `01 E0 FC 02 AA 55`** (reply `04 0E 05 01 E0 FC AA 55`). The official Beken ToolKit
sends this second handshake. Earlier open scripts didn't. Full command table: [protocol.md](skills/bk3266/references/protocol.md).

## Documentation

| Topic | File |
|---|---|
| Agent workflow and safety rules | [SKILL.md](skills/bk3266/SKILL.md) |
| UART bootloader protocol | [protocol.md](skills/bk3266/references/protocol.md) |
| Flash layout, CRC records, voice prompts | [flash-format.md](skills/bk3266/references/flash-format.md) |
| Config field map with confidence | [config-map.md](skills/bk3266/references/config-map.md) |
| Disassembly and navigating the code | [reverse-engineering.md](skills/bk3266/references/reverse-engineering.md) |
| Why it disconnects when audio stops | [disconnects.md](skills/bk3266/references/disconnects.md) |
| Wiring and finding pads | [hardware.md](skills/bk3266/references/hardware.md) |

## Tests

```sh
python3 -m unittest discover -s tests
```
The tests are offline. They include a simulated bootloader that exercises `dump` and `write` end to end.

## What's not here

No firmware images, voice clips or Beken ToolKit binaries. They belong to their vendors, and dumps contain
per-device pairing data. `.gitignore` blocks `*.bin`. The [pyba2](https://github.com/alephsecurity/pyba2) BA2 decoder is
fetched on first use.

## Disclaimer

Flashing can brick a device. Practice on a spare board, keep two matching backups, and only write sectors you
understand. This project isn't affiliated with Beken. Provided as-is under the MIT license.
