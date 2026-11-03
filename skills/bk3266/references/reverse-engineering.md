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

1. **Debug strings are the index.** The firmware is full of `printf`-style strings even though UART logging is off.
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

Other builds will have different addresses but the same strings. Re-derive them from the strings.

## Writing up findings

Label every claim as **verified** (read back, or traced in code) or **inferred** (ToolKit name, arithmetic fit).
Hand-written C-like reconstructions are explanation aids, not source code.
