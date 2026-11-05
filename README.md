# BK3266 firmware tools: dump, decode, and change the config of Beken BK3266 Bluetooth chips over UART

Open-source tools and documentation for the **Beken BK3266** Bluetooth audio SoC, the chip inside many inexpensive
Bluetooth headphones, sleep headbands, speakers, car kits and audio receivers. With them you can:

- connect to the **BK3266 ROM bootloader** over a 3.3 V USB-serial (FTDI) adapter from macOS, Linux or Windows
- **download / back up / dump the full SPI flash firmware**, with no Windows-only Beken ToolKit needed
- **decode and change configuration settings**: voice prompts, low-battery warning, volumes, sleep and power-off
  timeouts, auto-reconnect, sniff / power saving
- **mute or replace voice prompts** ("battery low", "connected", "disconnected", "power on", "pairing")
- **disassemble the firmware**: the BK3266 core is **BA2 (Beyond Architecture 2), not ARM**
- **turn on the firmware's hidden debug log** (one config bit) and read it over the same UART wires
- **patch code safely**: byte-exact replacement with old-bytes check, CRC repair and disassembly read-back
- diagnose **Bluetooth disconnect / reconnect loops** from an iPhone PacketLogger capture and the headset's own log

The project is a single Python script (`bk3266.py`) plus an **agent skill** for Claude Code, OpenAI Codex CLI and GitHub
Copilot CLI. The skill teaches an AI coding agent the protocol and the safety rules.

> **Status:** reading flash, decoding the config, writing config and code changes, rebooting out of download mode,
> the debug UART log and the disconnect-loop fix are all verified on real hardware (BK3266, 512 KiB Puya flash, firmware built 2021). Other flash sizes or firmware builds may need `--config-base`. Check the
> [field confidence notes](skills/bk3266/references/config-map.md) before writing.

---

## 1. How to connect to a BK3266 (wiring)

| BK3266 board | USB-serial adapter (3.3 V) |
|---|---|
| UART **TX** pad | adapter **RX** |
| UART **RX** pad | adapter **TX** (330 Ω–1 kΩ series resistor recommended) |
| GND (battery −) | GND |
| — | **VCC: leave disconnected.** Power the board from its own battery. |

- **Use a 3.3 V adapter. 5 V logic can damage the chip.**
- Many BK3266 boards expose UART as **two small unlabeled round test pads next to the chip**. If nothing answers, swap TX and RX.
- **Shipping firmware prints nothing on UART.** A silent line doesn't mean the wiring is wrong.
- Serial settings: **115200 baud, 8N1**.

More: [hardware.md](skills/bk3266/references/hardware.md).

## 2. How to enter the BK3266 bootloader (download mode)

The ROM bootloader listens only briefly at power-on:

1. The host continuously sends **LinkCheck** `01 E0 FC 01 00`.
2. **Press and hold the power button.** The chip replies `04 0E 05 01 E0 FC 01 00`.
3. **Immediately send the second handshake** `01 E0 FC 02 AA 55`. The reply is `04 0E 05 01 E0 FC AA 55`.
4. The session is now stable and the button can be released.

> **Why "BK3266 bootloader exits after one read" happens:** without step 3, the bootloader serves a single 4 KB
> read and returns to the application about 0.5 s later. The official Beken ToolKit (V5.8.6) sends this AA/55
> handshake; earlier open-source scripts didn't.

```sh
pip install pyserial
python3 skills/bk3266/scripts/bk3266.py ping --port /dev/cu.usbserial-XXXX --say   # Linux: /dev/ttyUSB0, Windows: COM5
```
`ping` prints the flash JEDEC ID, the capacity, and the flash write-protection status.

### "My BK3266 headphones won't turn on after flashing": leaving download mode

Once the second handshake is done, **the chip stays in download mode until it loses power**. With a soldered battery, the
headset then looks dead: no LEDs, no charge light, and the power button does nothing. The fix is the reboot frame
**`01 E0 FC 02 0E A5`**, which the Beken ToolKit sends 3× after a write. `bk3266.py` sends it at the end of every successful
session. To rescue a stuck chip:
```sh
python3 skills/bk3266/scripts/bk3266.py reboot --port /dev/cu.usbserial-XXXX
```
Tell-tale sign: the chip ACKs LinkCheck instantly **without** the power button being pressed.

## 3. How to download (dump) the BK3266 firmware

```sh
python3 skills/bk3266/scripts/bk3266.py dump --port /dev/cu.usbserial-XXXX --say --out backups
```
This reads the whole flash **twice** and checks that both reads match byte for byte. It saves `flash-dump-1.bin` and
`flash-dump-2.bin`, a `manifest.json` with SHA-256 hashes, and a full TX/RX log. A 512 KiB part takes about a minute per pass at 115200.

### BK3266 bootloader command reference

Extended frame: `01 E0 FC FF F4 <len:u16 LE> <cmd> <args…>`. Reply: `04 0E FF 01 E0 FC F4 <len:u16 LE> <cmd> <status> <payload…>`. Status `00` = OK.

| Cmd | Operation | Args | Reply payload |
|---|---|---|---|
| `09` | **Read 4 KiB** | `addr:u32 LE` | `addr` + 4096 bytes |
| `0B` | **Erase 4 KiB sector** | `addr:u32 LE` | `addr` |
| `07` | **Write 4 KiB sector** | `addr:u32 LE` + 4096 bytes | `addr` |
| `0C` | Read flash status register | `05` (SR1) / `35` (SR2) | `reg, value` |
| `0D` | Write flash status registers | `01 SR1 SR2` | echo |
| `0E` | SPI passthrough (e.g. JEDEC `9F 00 00 00`) | 4 bytes | 4 bytes, first one a dummy byte |

Short frames: `01 E0 FC 01 00` LinkCheck, `01 E0 FC 02 AA 55` stay-in-download-mode handshake, `01 E0 FC 02 0E A5` **reboot** (no ACK).

Example: a full read of block 0 is `01 E0 FC FF F4 05 00 09 00 00 00 00`.

**Write protection:** the Puya flash seen ships with SR1/SR2 = `00 40` (whole array protected). Write `00 00` before
erasing or programming, then **restore the original value**. Reads work while the flash is protected.
Full protocol notes, with the ToolKit addresses each command was found at: [protocol.md](skills/bk3266/references/protocol.md).

## 4. BK3266 flash format (CRC every 32 bytes)

The firmware image is stored as **34-byte records: 32 data bytes followed by a CRC16**. The CRC uses poly `0x8005`, init `0xFFFF`,
no reflection and no final XOR, and is stored big-endian (`0x8029` = CRC of 32 zero bytes). Address conversion:
`raw = (logical // 32) * 34 + logical % 32`.
**Any edit must recompute the CRC of each record it touches.** `bk3266.py patch` does this for you.

| Region (512 KiB part) | Raw offset | Logical |
|---|---|---|
| Program code (BA2) | `0x00000`… | `0x00000`… |
| Voice prompts (SBC audio) | … | `0x5DE00`… |
| **Configuration** | **`0x7C500`** | **`0x75000`** |
| Paired-device log (rewritten by the firmware whenever it runs) | `0x7E000` | not CRC-paged |
| Per-device environment / pairing record | `0x7F000` | not CRC-paged; **preserve** |

More: [flash-format.md](skills/bk3266/references/flash-format.md).

## 5. How to read and change BK3266 configuration settings

```sh
S=skills/bk3266/scripts/bk3266.py
python3 $S config backups/dump-*/flash-dump-1.bin                 # decode every known setting + prompt table
python3 $S patch  backups/dump-*/flash-dump-1.bin -o new.bin \
        --mute-prompt low-battery --set vol_wave=6                # offline: CRCs fixed, byte diff printed
python3 $S write new.bin --expect backups/dump-*/flash-dump-1.bin --port /dev/cu.usbserial-XXXX          # dry run
python3 $S write new.bin --expect backups/dump-*/flash-dump-1.bin --port /dev/cu.usbserial-XXXX --yes    # write
```

`write` safety:
- Before writing, it **re-reads the whole chip and aborts unless it matches `--expect`**.
- It erases and writes **only the 4 KiB sectors that changed**, verifies each one twice, restores the original write protection, then re-reads the full image to confirm.
- It refuses sectors outside the config region unless you pass `--allow-any-sector`.
- It reboots the chip out of download mode when it's done.

**Use a fresh backup.** The firmware appends to its paired-device log every time the headset runs, so a backup from
yesterday won't match, and `write` will (correctly) refuse. Run `dump --no-reboot` right before `patch` and `write`.

### Common BK3266 settings (offset from config base)

| Setting | Offset | Type | Meaning |
|---|---:|---|---|
| `vol_a2dp` / `vol_hfp` / `vol_wave` | `0x09` / `0x0A` / `0x0B` | u8 | default music / call / **voice-prompt volume** |
| `lp_interval` | `0x0E` | u32 | battery check interval, 10 ms ticks (6000 = 60 s) |
| `lp_threshold` | `0x12` | u16 | **low-battery warning** threshold (≈ mV, e.g. 3300) |
| `lp_pd_threshold` | `0x14` | u16 | low-voltage **shutdown** threshold (≈ mV, e.g. 3100) |
| `sleep_timeout` | `0x22` | u16 | idle time before sniff / low power, 10 ms ticks. **Never 0: that fires immediately** |
| `powerdown_timeout` | `0x24` | i32 | auto power-off when not connected, 10 ms ticks |
| `bt_flag` | `0x59` | u32 | `0x10` auto-reconnect, `0x20` link-loss recovery |
| `disconn_retry_count` / `disconn_retry_interval` | `0x99` / `0x9A` | i8 / u32 ms | reconnect attempts (-1 = unlimited) / interval |
| `auto_conn_interval` | `0x9F` | u16 ms | auto-reconnect interval |
| `wave_lang_sel` | `0xE5` | u8 | voice-prompt language bank |
| `wave_info[42]` | `0x424` | 3 B each | voice prompt table: `type, page:u16`; **type 0 = muted** |
| `feature_flag` | `0x34A` | u32 | bit `0x80000` = **sniff (Bluetooth power saving)** on 2021 firmware |

Full map with confidence levels: [config-map.md](skills/bk3266/references/config-map.md).

### Recipes

| I want to… | Command |
|---|---|
| **Disable the "battery low" voice prompt** | `patch --mute-prompt low-battery` |
| Mute the connected / disconnected announcements | `patch --mute-prompt connected --mute-prompt disconnected` |
| Make all voice prompts quieter | `patch --set vol_wave=5` |
| **Stop it disconnecting ~30 s after audio stops** (confirmed fix) | `patch --clear-bits feature_flag=0x80000` (see [§7](#7-why-do-bk3266-headphones-disconnect-and-reconnect-when-audio-stops)) |
| **Turn on the firmware's debug log** (115200 8N1 on the flashing pads) | `patch --set-bits system_flag=0x1`, then `listen --port …` |
| Replace exact code bytes (refuses if the old bytes differ) | `patch --poke 0x2bb6f=2481da:2c815a`, then `write … --allow-any-sector` |
| Lengthen the idle timer instead | `patch --set sleep_timeout=12000` (120 s) |
| Extract the voice prompts as WAV | `prompts dump.bin -o voice-prompts` (needs ffmpeg) |

## 6. Disassembling BK3266 firmware (BA2, not ARM)

```sh
python3 $S strip dump.bin -o logical.bin        # drop CRC bytes
python3 $S disasm logical.bin -o listing.asm    # fetches alephsecurity/pyba2 on first use
python3 $S info dump.bin --strings              # debug strings: the best map of the code
```
Ghidra has no stock BA2 module. Navigate by the firmware's debug strings and by HCI opcode immediates.
More: [reverse-engineering.md](skills/bk3266/references/reverse-engineering.md).

### The BK3266 debug UART log

Production firmware is silent on UART, but the logger is still there. On 2021 firmware, `system_flag` bit `0x1`
(config `+0x01`) gates the UART init at 115200 8N1 on the same pads as the bootloader. Set it, and the headset prints its
Bluetooth stack's own trace: `sniff enable`, `enter sniff`, `ep.unit_mode=…`, `disconn_compl … 0x22`, battery millivolts and more.
```sh
python3 $S patch dump.bin -o debug.bin --set-bits system_flag=0x1     # 3 bytes, undo with --clear-bits
python3 $S listen --port /dev/cu.usbserial-XXXX --grep 'sniff|unit_mode|disconn'
```
With it on, running firmware also ACKs the LinkCheck frame. Switch the headset off before catching the bootloader.
Details: [debug-uart.md](skills/bk3266/references/debug-uart.md).

## 7. Why do BK3266 headphones disconnect and reconnect when audio stops?

**Diagnosed and fixed on real hardware (iPhone and a BK3266 sleep headband):**

- **Cause:** about 30 s after audio stops, the firmware's idle timer (`sleep_timeout`) switches the link policy to allow Bluetooth
  **sniff mode**. When the iPhone then asks for sniff (240 ms, attempt 1, timeout 1), the headset's link manager, which only accepts
  320–400 ms with attempt ≥ 4, **counter-proposes an invalid 4-slot interval**. The iPhone rejects that (`0x1E`, visible in the
  headset's debug log), and its own request is never answered. Exactly 30 s later the phone drops the link with HCI reason **`0x22`
  LMP Response Timeout**. Auto-reconnect turns that into a "disconnected… connected…" loop.
- **Proper fix (keeps power saving):** patch the link manager to accept the phone's parameters (15–400 ms, attempt ≥ 1), as
  Apple's accessory guidelines require. Anything outside that range gets a *valid* counter-proposal, and the stock clean refusal
  follows after 4 rounds. That's one instruction plus a rewritten 39-byte clamp on the 2021 build. Verified with an iPhone: accepted every time,
  minutes-long sniff, no drops, and a valid counter-proposal is declined cleanly by iOS instead of hanging the link. See [disconnects.md](skills/bk3266/references/disconnects.md#fixed-properly-the-headset-now-accepts-the-phones-sniff-2026-11-04).
- **Simple fix (config only):** clear one config bit, `feature_flag` `0x80000`. The headset then refuses sniff straight away (`status 0x24`),
  iOS stops asking, and the link stays up. In testing: 20 minutes idle, zero drops.
  ```sh
  python3 $S dump  --port /dev/cu.usbserial-XXXX --no-reboot --out backups      # fresh baseline, stay in download mode
  python3 $S patch backups/dump-*/flash-dump-1.bin -o fixed.bin --clear-bits feature_flag=0x80000
  python3 $S write fixed.bin --expect backups/dump-*/flash-dump-1.bin --port /dev/cu.usbserial-XXXX --yes
  ```
  This is 3 bytes in the config sector. The trade-off is somewhat higher idle current while connected but silent. Streaming is unaffected.

To check whether your device has the same failure, capture the HCI disconnect reason with iPhone PacketLogger or the
Android HCI snoop log, then:

```sh
python3 $S pklg capture.pklg                              # all events + a "Devices seen" table to identify the headset
python3 $S pklg capture.pklg --device 27:72:1c:9c:a9:ff   # only the headset: sniff requests, mode changes, disconnect reasons
```
A phone capture includes every device the phone talks to (watch, earbuds, car). Identify the headset in the summary first:
BK3266 parts report LMP `0x09` and manufacturer `0x000E`. Apple devices report `0x004C`.
Step-by-step capture guide, the side-by-side phone/headset logs, and every experiment tried so far:
[disconnects.md](skills/bk3266/references/disconnects.md).

---

## Install as an AI agent skill

### Claude Code
```sh
claude plugin marketplace add dfrysinger/bk3266 --scope user
claude plugin install --scope user bk3266@bk3266
```
### OpenAI Codex CLI
```sh
codex plugin marketplace add dfrysinger/bk3266
codex plugin add bk3266@bk3266
```
### GitHub Copilot CLI
```sh
copilot plugin install dfrysinger/bk3266
```

## Tests

```sh
python3 -m unittest discover -s tests
```
The tests run offline, using a simulated BK3266 bootloader that exercises `dump` and `write` end to end.

## What's not included

There are no firmware images, voice clips or Beken ToolKit binaries in this repo. They belong to their vendors, and
dumps contain per-device pairing data (`.gitignore` blocks `*.bin`). The BA2 decoder
[pyba2](https://github.com/alephsecurity/pyba2) is fetched on first use.

Related: the [BK3254 project](https://github.com/tomaskovacik/BK3254), where the ToolKit was shared (issue #20).

## Disclaimer

Flashing can brick a device. Practice on a spare board, keep two matching backups, and only write sectors you
understand. This project isn't affiliated with Beken Corporation. MIT license.
