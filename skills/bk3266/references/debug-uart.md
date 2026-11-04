# Turning on the firmware's own debug log

Production BK3266 firmware is silent on UART, but the debug logger is still compiled in, with hundreds of `printf`
strings. On the 2021 "Cozy" image the only thing switching it off is **one config bit**. With it on, the headset narrates
its own Bluetooth stack on the same two wires you use for flashing. That turns "guess from the phone's capture"
into "read the headset's reasoning".

## The switch: `system_flag` bit `0x1` (config `+0x01`)

Verified in code and on hardware (2026-11-04):

- The UART init at logical `0x3966C` runs only if `used == 1` **and** config byte `+0x01` has bit 0 set
  (`0x3965B`–`0x39669`). It writes UART config `0xE01B` to `0xF08000`: TX+RX enabled, 8 data bits, divisor `0xE0`.
  26 MHz / 225 ≈ **115200 baud, 8N1**.
- If the config block is missing (`used != 1`, `0x3A4F8`), the firmware enables the UART unconditionally, so the factory
  default is "debug on". The product config switches it off.
- No other code tests bit 0 of that word. The other readers of the `system_flag` word use bits 3, 4, 7, 8, 15 and `0x100`.
- The same pins carry the bootloader, so no extra wiring is needed.

```sh
python3 scripts/bk3266.py dump  --port $P --no-reboot --out backups      # fresh baseline, stays in download mode
python3 scripts/bk3266.py patch backups/dump-*/flash-dump-1.bin -o debug-on.bin --set-bits system_flag=0x1
python3 scripts/bk3266.py write debug-on.bin --expect backups/dump-*/flash-dump-1.bin --port $P --yes
python3 scripts/bk3266.py listen --port $P --grep 'sniff|unit_mode|disconn|stream'    # then power the headset on
```

This is a 3-byte write: the byte plus its record CRC. Undo it the same way with `--clear-bits system_flag=0x1`.
On another build, find the UART init by its `0xF08000` store and check what gates it before setting anything.

## What the log shows

```
system_flag: d881,1
jstart_stack version 4.0.33.5                  ← Jungo BTware host stack
| SW Build Time: 10:27:56, May 21 2021
| Bluetooth name: Cozy
bt_auto_connect_start,id1:1,id2:2,flag:0x80009
hci_event_con_compl:03
sniff disable:0,0032                           ← A2DP stream start
stream_start_handler:0, 44100, 2
stream suspen1
sniff enable:0,0032                            ← idle timer fired: link policy now allows sniff
enter sniff:0,0032                             ← headset sends HCI Sniff Mode
ep.unit_mode=2                                 ← HCI Mode Change event (stock line prints only the mode)
disconn_compl:0x415394,0x0032,0x22
bt_disconn_reason: 0x 22, raddr:14:1b:a0:db:fd:7b
+++adc:3384(mV),adc_avr:3384(mV)              ← battery, every 2 s
voice play low power!!!,lowpower_flag=1
```

The phone's PacketLogger trace is in UTC and the listener timestamps local time; once you allow for the zone, the two line up to the second.

## Printing the full Mode Change event (one-instruction code patch)

The stock `ep.unit_mode=%x` line (logical `0x2BB6F`) passes only the mode byte. The handler's event buffer is
`status, handle:u16, mode, interval:u16` at `r1+0x58`. Loading the first word instead prints all of it:

```
0x2bb6f  bn.lbz r4,0x5b(r1)  24 81 da   →   bn.lwz r4,0x58(r1)  2c 81 5a
python3 scripts/bk3266.py patch in.bin -o out.bin --poke 0x2bb6f=2481da:2c815a
```

The value is little-endian, so it reads `mode·handle·status`: `2003200` = mode 2 (sniff), handle `0x0032`, status `00`.
`321e` = mode 0, status **`0x1E`**. Only the log argument changes, and `r4` is dead after the logger call. The same
function already does `lwz r7,0x58(r1)` at `0x2BB2D`, so the load is aligned.

## Side effects seen

- **Running firmware with the debug UART enabled ACKs LinkCheck** (`04 0E 05 01 E0 FC 01 00`), but not the second
  handshake. A naive "catch" then succeeds instantly and fails on AA/55. `bk3266.py` now keeps trying until both
  handshakes answer, and tells you to switch the headset off first.
- The listener and the flasher can't share the port. Stop `listen` before any device command.
</content>
</invoke>
