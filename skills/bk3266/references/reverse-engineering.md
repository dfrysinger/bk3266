# Making BK3266 firmware legible

## Instruction set: BA2, not ARM

Some write-ups call the BK3266 an ARM968E-S part. The dumped firmware decodes cleanly as **BA2 (Beyond Architecture 2)**:
the startup code and the battery and prompt routines lift into sensible BA2 sequences. Decoding the same bytes as ARM gives nonsense.

- Decoder: [alephsecurity/pyba2](https://github.com/alephsecurity/pyba2), pinned commit `b61b6b50b025a49a6e324fe92d1de71651509ab4`.
  The repo has no standalone LICENSE file, so this skill **fetches it on first use** rather than bundling it.
- Stock Ghidra has no BA2 processor module, so there's no one-click whole-image decompile. Work at the routine level.

```sh
python3 scripts/bk3266.py strip dump.bin -o logical.bin
python3 scripts/bk3266.py disasm logical.bin -o listing.asm             # linear sweep 0..0x56700
python3 scripts/bk3266.py disasm logical.bin -o idle.asm --start 0x2FF5F --end 0x300A0
```

A linear sweep mixes code with embedded data. Treat it as a map, not a function list.

## Navigation strategy

1. **Debug strings are the index.** The firmware is full of `printf`-style strings even though UART logging is off by default.
   Run `info --strings` and look for handler names such as `app_hci_discon_compl_cb`, `bt_disconn_reason: 0x%x`, `sniff disable:%d`,
   `exit sniff`, `===sleep slots`, `icu_deepsleep`, `Reconnection after disconnection`, and the battery and wave messages.
2. Find the code that loads each string's logical address. Those are the handlers.
3. Config fields are read relative to a RAM base register. In the 2021 image the config RAM copy sits at
   `r2+0x78F2`, so `r2+0x7914` is `sleep_timeout` (+0x22) and `r2+0x7C3C` is `feature_flag` (+0x34A).
   Grep the listing for `0x79xx(r2)` / `0x7Cxx(r2)` offsets to find who uses a setting.
4. HCI opcodes appear as immediates: `0x0803` Sniff Mode, `0x0804` Exit Sniff, `0x080D` Write Link Policy, `0x0406` Disconnect.
5. Use the ToolKit EXE (x86, readable with any disassembler) as a **Rosetta stone** for field names, offsets and
   unit scaling. Its strings are in Chinese, e.g. `检测间隔(s)` = "detection interval (s)" and `sniff使能` = "sniff enable".
   The ToolKit has circulated publicly via the BK3254 project's issue #20 (github.com/tomaskovacik/BK3254/issues/20).
   It isn't redistributed here. It's unsigned, so run it only in a VM or on a spare Windows machine.

## Landmarks (2021 "Cozy" image, logical addresses)

| Address | What |
|---|---|
| `0x105DE` | debug logger; suppressed when `r2+0x8380 & 0x08000000`; TX byte to MMIO `0xF0800C`, status `0xF08008 & 0x100000` |
| `0x220CD` | sniff routine: builds HCI Sniff Mode (max `0x280`, min `0x200`, attempt 4, timeout 4) / Exit Sniff |
| `0x2654C` | sniff policy enable/disable (logs `sniff disable`), gated by `feature_flag & 0x80000` |
| `0x28764` | periodic tick adds elapsed time to idle counter `r2+0x140C` |
| `0x2FF5F` | idle path: `if (sleep_timeout > idle) return;` then per-link sniff entry (`0x30078`, `0x3008C`) |
| `0x2B2CA` / `0x2B30C` | HCI disconnection-complete handling (`disconn_compl`, reason byte) |
| `0x40BDC` | A2DP stream start: resets idle counter, disables sniff |
| `0x40D1E` | A2DP stream suspend |
| `0x41ECC` | app disconnect wrapper: logs `bt_disconn_reason`, plays prompt 2, reason-specific branches (`0x13/0x16`, `0x08/0x05`, `0x22`), reconnect at `0x42370` |

| `0x2B1AD` | on connection: link policy `0xC2(r12) |= 1` (role switch only), HCI Write Link Policy |
| `0x265B1` | the **only** place that sets policy bit `0x4` (allow sniff), in the enable half of `0x2654C` |
| `0x2BB21`–`0x2BB78` | HCI Mode Change handler: event at `r1+0x58` (`status, handle, mode, interval`), toggles the per-link "active" byte (`0x12A4 + 12*i`) only on status 0, logs `ep.unit_mode` |
| `0x3966C` / `0x3A6BD` | UART init (`0xE01B` → `0xF08000`, 115200 8N1), gated by `system_flag` bit 0 / `used != 1` |
| `0x30070` / `0x30084` | idle path per link: first tick calls policy-enable `0x2654C(h,1)`, next tick (if still active) `0x220CD(h,1)` enter sniff |

Other builds will have different addresses but the same strings. Re-derive them from the strings.

## Turn on the debug log before guessing

The single most useful step is enabling the firmware's own UART log ([debug-uart.md](debug-uart.md)). Search for the
UART init (store to `0xF08000`) and the config bit that gates it. Then every hypothesis can be checked against the headset's
own account, not just against what the phone saw.

## Patching code

Code patches use the same safety rails as config patches. They need `--allow-any-sector` at write time, and there's one
extra step: **disassemble the patched image and read it back** before showing anyone the diff.

1. Find the instruction, and confirm every other place that touches the same state (grep the listing for the offset or the bit).
2. Get the encoding from an existing instruction of the same form. **Don't hand-encode BA2 immediates**: they aren't stored
   plainly. In `bn.ori`/`bn.andi`/`bn.lbz` the 8-bit field is bit-reversed (`ori 0x1` → `80`, `ori 0x4` → `20`, `lbz 0x5b` → `da`),
   and `bn.lwz` uses yet another layout (`0x58` → `5a`). Pick one with the same register and opcode from the listing and copy it.
3. Keep the length the same, so nothing else moves. Changing only an immediate or a register field is the safest edit.
4. `patch --poke LOGICAL=OLDHEX:NEWHEX` refuses unless the old bytes match, then fixes the record CRC.
5. `disasm --raw patched.bin --start … --end …` must show the intended instruction with unchanged neighbours.
6. Start with patches that can't change behaviour (log arguments), so you exercise the write path on code sectors first.

Patches made so far (2021 "Cozy" image, all on the practice board):

| Logical | Old → new | Effect | Verdict |
|---|---|---|---|
| `0x2BB6F` | `24 81 da` → `2c 81 5a` (`lbz r4,0x5b(r1)` → `lwz r4,0x58(r1)`) | `ep.unit_mode` logs status, handle and mode | Works, log-only |
| `0x265B1` | `38 e7 20` → `38 e7 00` (`ori r7,r7,0x4` → `0x0`) | policy never allows sniff; headset still requests its own | Stable, but the controller rejects the headset's own sniff too (reverted) |
| `0x3D2A8`, `0x3D3BF`, `0x3D3C6`, `0x3D426` | (intermediate B+C) | LM accepts the phone's sniff (15–400 ms, attempt ≥ 1); never counter-proposes | Worked; superseded |
| `0x3D2A8` + 39-byte block `0x3D3AD` | see [disconnects.md](disconnects.md#fixed-properly-the-headset-now-accepts-the-phones-sniff-2026-11-04) | **final**: accept 15–400 ms / attempt ≥ 1, else a *valid* counter-proposal clamped into the window, else stock refusal | **Works**: iPhone accepted 5/5, no drops; a valid counter is declined cleanly by iOS |

Rewriting a block in place (A) is safer than a code cave on this image. The zero runs are either inside data tables
(`0x566D5`, just before the `BK326601` template read at `0x394BB`) or in sector 0 ahead of a boot header with magic numbers at `0xF20`.
Check every jump into the block first (`grep` the listing for target addresses inside it).

### The link manager (controller) is in flash too

| Address | What |
|---|---|
| `0x56880` | LMP opcode dispatch table (4-byte handler pointers; `0x32313` = unsupported). Entry 23 = sniff_req, 43 = SCO_link_req |
| `0xA3AA` | dispatcher: calls the handler; a non-zero return is sent back as LMP_not_accepted (`0x236AB`) |
| `0x3D217` | LMP_sniff_req handler (accept / counter-propose via `0xD0D0` / reject `0x20` at `0x3D444`) |
| `0x3CB4F` | HCI Sniff Mode command handler (checks link policy `0x1D4 & 4`, else `0x0C`) |
| `0x3C517` | controller version setup: LMP 9, subversion `0x1450`, manufacturer `0x000E` |

Finding LMP handlers: search the logical image for a handler address stored as a little-endian 32-bit word. That lands in the
dispatch table, and the table index is the LMP opcode.

### Assembling instead of hand-encoding

pyba2 can assemble (`isa.instructions.Instruction.lower(text, addr).encode()`). First re-assemble the *existing* instruction
and check it matches byte for byte. Pass each instruction's real address, because branches are PC-relative. The
assembler may choose a shorter form (`bg.j` came out as 2-byte `bt.j`; `addi -0x18` as 3-byte `bn.addi`). Pad with
`bt.nop` (`00 10`), or keep the long form by editing the immediate in the original encoding: the 16-bit immediate of
`bg.addi` is bit-reversed (`-0x200` → `00 7f`, `-0x18` → `17 ff`). Decode every result before using it.

## Writing up findings

Label every claim as **verified** (read back, or traced in code) or **inferred** (ToolKit name, arithmetic fit).
Hand-written C-like reconstructions are explanation aids, not source code.
