# BK3266 ROM bootloader UART protocol

115200 8N1. Frames are HCI-style. Host→chip frames start `01 E0 FC`; chip→host replies start `04 0E`.
Source: static analysis of Beken *BK3266 ToolKit V5.8.6 C (2018-08-14)*. The VAs below are in
`ToolKitV5_8_6_C_20180814.exe`, SHA-256 `380a656a50dd5bec02863454306e9e7b9dc85fad5b6188014a354fa00b97bb52`.
All of it was confirmed on hardware.

## Entering download mode

| Step | TX | Expected RX |
|---|---|---|
| 1. LinkCheck, sent every ~10 ms while the owner presses and holds power | `01 E0 FC 01 00` | `04 0E 05 01 E0 FC 01 00` |
| 2. Second handshake, sent **immediately** | `01 E0 FC 02 AA 55` | `04 0E 05 01 E0 FC AA 55` |

The ToolKit's startup routine `0x443CE0` (and the alternate connect `0x44B520`) calls the LinkCheck wrapper `0x404230`,
then the AA/55 wrapper `0x4042C0`. Each wrapper loops until it gets a reply. The AA/55 builder is at `0x402310`, its validator at `0x402EA0`, and the per-attempt timeout is 5 ms.
**If you skip step 2, the bootloader serves one read and drops back to the application ~0.5 s later.**
After step 2 the session is stable through idle gaps, and the button can be released.

## Extended commands

```
TX: 01 E0 FC FF F4 <len:u16 LE> <cmd> <args…>          len = 1 + len(args)
RX: 04 0E FF 01 E0 FC F4 <len:u16 LE> <cmd> <status> <payload…>   status 00 = OK
```

| Cmd | Name | Args | Reply payload | ToolKit builder / validator |
|---|---|---|---|---|
| `09` | Read 4 KiB | `addr:u32 LE` | `addr:u32` + 4096 bytes (reply len `0x1006`, 4111 bytes on wire) | `0x4025E0` / `0x4032A0` |
| `0B` | Erase 4 KiB sector | `addr:u32 LE` | `addr:u32` | `0x4023C0` / `0x403080` |
| `07` | Program 4 KiB sector | `addr:u32 LE` + 4096 bytes (len `0x1005`) | `addr:u32` | `0x402520` / `0x4031F0` |
| `0C` | Read SPI status register | `05` (SR1) or `35` (SR2) | `reg, value` | `0x402830` |
| `0D` | Write SPI status registers | `01 SR1 SR2` | echo `01 SR1 SR2` | Puya path `0x44B480` |
| `0E` | SPI passthrough | e.g. `9F 00 00 00` (JEDEC ID) | 4 SPI bytes; **byte 0 is a dummy byte** | parsed at `0x403760` |

Quirks:
- The `0E` reply header advertises length 5 but carries 6 bytes (cmd, status and 4 SPI bytes).
- JEDEC: discard the first byte. For example, `FF 85 60 13` means Puya (`85`), type `60`, capacity 2^0x13 = 512 KiB. The ToolKit shifts the word right by 8 at `0x4499FE`.
- Erase leaves `FF`. Programming can only clear bits, so always erase a sector before rewriting it.

## Write protection

The Puya parts seen ship with **SR1=`00`, SR2=`40`**: CMP=1 with BP=0, which protects the whole array.
To program, write `00 00`, verify it, then **restore the original value exactly**. The ToolKit
dispatches Puya ID `0x136085` to `0x44B480`, which does the same. Reads work while the flash is protected. Never touch the OTP or security
registers. For other flash vendors, read the datasheet's BP/CMP semantics before choosing an unlock value
(`--unprotect-value`, `--unprotect-value-confirmed`).

## Leaving download mode: reboot `01 E0 FC 02 0E A5`

**Once the AA/55 handshake has been done, the chip stays in download mode for as long as it has power.** On a board
with a soldered battery, that means until the battery runs flat. While it's stuck there, the headset looks dead: no LEDs,
no charge light, and the power button does nothing. A LinkCheck still gets an instant ACK **without** the button being
pressed, which is how you tell.

The ToolKit sends `01 E0 FC 02 0E A5` three times after a write (builder `0x402270`, caller `0x449DBB`). Verified on hardware
(2026-11-03): after it, the chip stopped answering LinkCheck, a single `00` byte appeared on the line, and the headset
then powered on normally. It gets no ACK. `bk3266.py` sends it at the end of every successful `ping`, `dump` and `write`,
but **not** after a failed write, so you can retry or restore. Use `--no-reboot` to stay in download mode. The standalone
`reboot` command rescues a chip that was left there.

## Other commands seen in the ToolKit (don't send these casually)

| Frame | Where | Status |
|---|---|---|
| `01 E0 FC 06 0F <baud:u32 LE> <delay>` | builder `0x402110`, wrapper `0x404350` | Baud change (115200, 1M or 2M). Not needed. |
| `01 E0 FC 05 FE 95 27 95 27` | builder `0x4022B0` | Sent on failed connects. Unknown. |

**Checking that the reboot worked:** the bootloader listens briefly at every reset, so a LinkCheck probe sent right after
the reboot can catch it again. `reboot` waits 3 s in silence first, then requires **both** handshakes before it concludes
"still in download mode". Running firmware with the debug UART on answers LinkCheck too (see below).

## Running firmware answers LinkCheck when its debug UART is on

With `system_flag` bit `0x1` set, the application firmware ACKs `01 E0 FC 01 00` with the same `04 0E 05 01 E0 FC 01 00`,
but ignores AA/55, and prints log text such as `+++adc:3334(mV)…`. An instant ACK followed by printable text means
**the headset is on**. Switch it off, then press and hold to catch the real bootloader. `connect()` handles this: it retries
until both handshakes answer, and says so if it sees log text.

## Practical timing

- The catch window is short and it can take several presses. Spam LinkCheck continuously and let the owner keep trying.
  `bk3266.py` listens for 5 minutes.
- **Catch from a powered-off headset.** Pressing to reset a running headset into the bootloader can interrupt a
  pairing-log write and leave weakly programmed bits (see [flash-format.md](flash-format.md)).
- A chip already in download mode answers within milliseconds. The tool then skips the "press and hold" cue.
- A full 512 KiB read takes about 60 s at 115200.
