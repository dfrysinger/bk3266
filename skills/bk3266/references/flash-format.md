# Flash layout and integrity

## 34-byte records

The image region is stored as records of **32 data bytes followed by a 2-byte CRC16**:
- polynomial `0x8005`, init `0xFFFF`, not reflected, no final XOR
- stored **big-endian**
- `0x8029` is the CRC of 32 zero bytes, which is a handy sanity check
- verified across 15,036 consecutive records of a real dump

Address conversion:

```
raw     = (logical // 32) * 34 + logical % 32
logical = (raw // 34) * 32 + raw % 34        # only valid when raw % 34 < 32
```

"Logical" addresses are what the CPU sees and what the code references. "Raw" addresses are flash offsets.
`bk3266.py strip` produces the logical image.

**Every data edit must recompute the CRC of each record it touches**, which means rewriting the containing 4 KiB
sector. A one-byte setting change is typically 3–4 raw bytes: the data byte(s) plus that record's CRC.
Records straddle sector boundaries, so preserve every other byte of the sector exactly.

## Regions (512 KiB part, 2021 firmware)

| Raw | Logical | Contents |
|---|---|---|
| `0x00000`… | `0x00000`… | Program. Block 0 contains ASCII `BK3266` near `0x10E`. Not encrypted. |
| … | ~`0x56700` | End of code region (approximate). |
| … | `0x5DE00`… | Prompt SBC clips (located through `wave_info` page pointers) |
| `0x7C500` | `0x75000` | **Main configuration** (`0x77F` bytes, `used` = 1, includes device name) |
| ~`0x7CFE4` | | End of the CRC-valid run |
| `0x7E000` | — | Paired-device / link-key log, not CRC-paged. Loaded into RAM at boot (`0x392DD`; `0xFE000` on 1 MiB parts). **The firmware appends to it whenever it runs**, so re-dump before every write. |
| `0x7F000` | — | Environment record (`BK326601`), not CRC-paged. **Preserve it.** It holds pairing and calibration data. |

`bk3266.py info` reports the CRC-valid extent of any dump.

## Voice prompts

- `cfg.wave_info` is at config `+0x424`: 42 three-byte records `type:u8, page:u16 LE`. The clip starts at logical `page * 256`.
  Type `0` means unused. The player's validation rejects unused slots, so the prompt is silently skipped.
- Five banks of `0x7E` bytes each, selected by `wave_lang_sel`. Usually only bank 0 is populated.
- Clip container: `total_len:u32 LE` (including those 4 bytes), followed by SBC frames. The ones seen are 40-byte frames, mono,
  32 kHz header, bitpool 16, sync bytes `9C 73 10`. `bk3266.py prompts` extracts them and decodes them with ffmpeg.
  The headphones' actual playback rate hasn't been measured, so previews may sound fast or slow.
- Slot names: 0 power-on, 1 connected, 2 disconnected, 3 power-off, 4 pairing, 5 low-battery, 6 HFP ring,
  13 vol-max, 14 vol-min, 18–27 digits 0–9 (from the ToolKit event table at EXE file offset `0xBB924`).

### Replacing a clip (not yet done on hardware)

To stay safe, encode a replacement SBC stream with the same parameters (mono, 32 kHz, bitpool 16,
matching frame size) that **fits within the original clip's allocation**, and update its length word. Then fix the CRCs of every
record you touch. Short beeps or silence fit easily. Relocating clips means rewriting page pointers and many sectors
outside the config region (`--allow-any-sector`), so treat that as a code-region-risk change.
