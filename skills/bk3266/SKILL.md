---
name: bk3266
description: Read, back up, decode, patch and write the SPI flash of Beken BK3266 Bluetooth audio chips (headphones, speakers, sleep headbands, cheap receivers) over a 3.3 V UART, and diagnose their Bluetooth behaviour. Use when someone wants to dump or back up BK3266 firmware, change its configuration (voice prompts, low-battery warnings, volumes, timeouts, reconnect behaviour, sniff/power saving), mute or replace voice prompts, disassemble the BA2 code, or work out why a BK3266 device disconnects, plus when they mention Beken, BK3266, BK3254-family ToolKit, or `01 E0 FC` bootloader frames.
---

# BK3266 flash and firmware

Everything goes through one CLI: `scripts/bk3266.py` (Python 3.9+, `pip install pyserial` for device commands).
Run `python3 scripts/bk3266.py <command> -h` for options. Device commands write an evidence directory
with a full TX/RX log and `manifest.json`; keep those.

## Safety rules (non-negotiable)

1. **No write without two identical full backups** (`dump` does this) **and the owner's explicit approval of the exact byte diff.**
2. **Practice board first** when the owner has a spare; never the only unit.
3. **Prefer data over code.** `write` refuses sectors outside the config region unless `--allow-any-sector`. A code patch
   additionally needs the patched bytes disassembled and read back before the owner sees the diff
   ([references/reverse-engineering.md](references/reverse-engineering.md#patching-code)).
4. **`write` always starts as a dry run.** It re-reads the whole chip and aborts unless it equals `--expect`, writes only changed 4 KiB sectors, verifies each twice, restores the flash's original protection bits, and re-reads everything.
5. Never set a timer to 0 to "disable" it until the firmware's comparison has been read. `sleep_timeout=0` fires immediately (the CLI refuses it).
6. Separate **verified** (read back, traced in code) from **inferred** (field names from the 2018 ToolKit) when reporting to the owner.
7. **Never leave the chip in download mode.** It stays there until it loses power, and a battery board then looks dead
   (no LEDs, no charge light, button ignored). Device commands reboot it on success. If a headset "won't turn on" after a session,
   run `reboot` before suspecting the flash: an instant LinkCheck ACK with no button press means it's still in download mode.
8. **Catch the bootloader from a powered-off headset.** Resetting a running one can interrupt its pairing-log write.
9. **Diagnose from the headset's own log before patching behaviour.** Turn on the debug UART ([references/debug-uart.md](references/debug-uart.md)).

## Working with the owner

The person holding the board usually isn't a firmware specialist and has to press a button at the right moment.
Give one physical action at a time, say exactly when to press and release, and use `--say` on macOS for spoken cues.
The bootloader catch waits up to 5 minutes (`--wait`), so nobody needs to rush.

## Hardware hookup

3.3 V USB-UART (FTDI or similar): adapter RX → chip TX, adapter TX → chip RX, GND → board ground. **Don't connect adapter VCC**; power the board from its own battery.
A 330 Ω–1 kΩ series resistor on adapter TX is cheap insurance. Many boards expose two unlabeled round test pads
beside the chip; finding which is TX/RX is covered in [references/hardware.md](references/hardware.md).
Production firmware is normally silent on UART, so silence doesn't mean the wiring is wrong.

## Core workflow

```sh
S=scripts/bk3266.py
python3 $S ping  --port /dev/cu.usbserial-XXXX --say          # catch bootloader; JEDEC ID + protection status
python3 $S dump  --port /dev/cu.usbserial-XXXX --say --out backups   # two full reads, must match
python3 $S info  backups/dump-*/flash-dump-1.bin --strings     # CRC extent, config location, debug strings
python3 $S config backups/dump-*/flash-dump-1.bin              # every known setting + prompt table
python3 $S patch  original.bin -o candidate.bin --mute-prompt low-battery   # offline, CRCs fixed, prints diff
python3 $S write candidate.bin --expect original.bin --port ...            # dry run: shows sectors and bytes
python3 $S write candidate.bin --expect original.bin --port ... --yes      # after owner approves the diff
python3 $S reboot --port /dev/cu.usbserial-XXXX                # rescue a chip left in download mode
python3 $S patch  dump.bin -o debug.bin --set-bits system_flag=0x1        # turn on the firmware's debug UART log
python3 $S listen --port /dev/cu.usbserial-XXXX --grep 'sniff|unit_mode|disconn'   # read it (stop before flashing)
python3 $S patch  dump.bin -o code.bin --poke 0x2bb6f=2481da:2c815a      # code patch: old bytes must match
python3 $S disasm --raw code.bin -o check.asm --start 0x2bb51 --end 0x2bb84   # read the patch back
```

`listen` and the device commands can't share the port: stop the listener first. If the pairing log has unstable bits
(the same `0x7E000`–`0x7EFFF` byte reads differently on different passes), pass `write --ignore-range 0x7e000-0x7f000`. It refuses to
cover a sector being written.

**The firmware writes its own pairing log (raw `0x7E000`–`0x7EFFF`) every time it runs and connects**, so an older backup
stops matching and `write` aborts with `device flash != --expect`. That abort is correct. Re-baseline in a single
download-mode session: `dump --no-reboot`, `patch` that fresh dump, then `write` against it. The chip is still in download
mode, so the firmware can't change anything in between.

To catch the bootloader, the host spams LinkCheck while the owner **presses and holds power**. Then it sends the **second
handshake `01 E0 FC 02 AA 55`**. Without that handshake the bootloader quits ~0.5 s after the first read, which is the classic
"I can only ever read one block" failure. Protocol details: [references/protocol.md](references/protocol.md).

## Common goals → what to change

| Goal | Change | Notes |
|---|---|---|
| Silence one prompt (e.g. low battery) | `--mute-prompt low-battery` | Sets that slot's type byte to 0; player rejects unused slots. Shutdown voltage logic untouched. |
| All prompts quieter | `--set vol_wave=N` | Global; index, not dB. |
| Different / clearer prompts (chimes, speech) | `scripts/make_prompts.py OUT`, listen to `OUT/*.wav`, then `patch --replace-prompt power-on=OUT/power-on.sbc …` | Verified on hardware. Clips play at **8 kHz** (labelled 32 kHz). The tool enforces format and room per slot. See [references/flash-format.md](references/flash-format.md). |
| Change LED patterns | `config` prints the LED event table (config `+0xE9`) | Plain data. Layout in [references/config-map.md](references/config-map.md). |
| Low-battery threshold/interval | `lp_threshold`, `lp_pd_threshold`, `lp_interval` | Muting is usually the better fix. |
| Drops ~30 s after audio stops, reason `0x22` | `--clear-bits feature_flag=0x80000` | **Confirmed fix** on 2021 firmware with an iPhone. Root cause, from the headset's log: its link manager rejects the iPhone's sniff parameters (`0x1E`) without telling the phone. See [references/disconnects.md](references/disconnects.md). |
| Drops fixed **and** keep idle power saving | 2 code patches in the link manager's sniff_req handler: accept the phone's parameters (15–400 ms, attempt ≥ 1), otherwise send a *valid* counter-proposal | Verified with an iPhone: accepted every time, minutes-long sniff, no drops. iOS declines counter-proposals cleanly. Recipe and addresses (2021 build): [references/disconnects.md](references/disconnects.md). Other builds: find the handler via the LMP dispatch table ([references/reverse-engineering.md](references/reverse-engineering.md)). |
| See what the headset is doing | `--set-bits system_flag=0x1`, then `listen` | Debug log at 115200 on the flashing pads. [references/debug-uart.md](references/debug-uart.md) |
| Reconnect loop behaviour | `disconn_retry_*`, `auto_conn_*`, `bt_flag` | Changes the aftermath, not the cause. |

Field meanings, offsets, units and confidence: [references/config-map.md](references/config-map.md).
The config base is known for 512 KiB flash (logical `0x75000`). For other sizes, find it (`used`=1 and the device name
inside the structure) and pass `--config-base`.

## Making the code legible

The CPU is **BA2 (Beyond Architecture 2), not ARM**. Ghidra has no stock BA2 support. Use `strip` to drop the CRC bytes,
then `disasm` (fetches [pyba2](https://github.com/alephsecurity/pyba2) at a pinned commit), and navigate by debug strings
(`info --strings`). Method and landmarks: [references/reverse-engineering.md](references/reverse-engineering.md).

## Diagnosing Bluetooth drops

`pklg` summarizes an Apple PacketLogger / iOS sysdiagnose capture: sniff requests, mode changes, and
**Disconnection Complete reason codes**. iPhone and Android capture steps are in [references/disconnects.md](references/disconnects.md).
**Phone captures include every device the phone talks to.** Read the closing "Devices seen" table, find the headset
(BK3266: LMP `0x09`, manufacturer `0x000E`), then re-run with `--device ADDR`, or `--handle H` for a link that was up before the
trace started. Don't diagnose from another device's drops; on iPhones, a paired Apple Watch is the usual noise.
