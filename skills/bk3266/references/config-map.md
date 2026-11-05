# Configuration field map

Offsets are relative to the logical config base (`0x75000` on 512 KiB parts). Integers are little-endian.
Names come from the Beken ToolKit V5.8.6's field-report routines (`0x460F00`, `0x461841`…, `0x461C84`…).
Unit scaling comes from its load and save paths. **The ToolKit dates from 2018. Firmware from 2021 moved at least one field**, so
confirm a field against the firmware's code before relying on it for anything unusual.

Confidence: **V** = verified in firmware code or by a hardware write; **T** = from ToolKit offsets and scaling only.

| Field | Off | Type | Units / meaning | Conf |
|---|---:|---|---|:-:|
| `used` | `0x000` | u8 | 1 = config active | V |
| `system_flag` | `0x001` | u32 | bitfield. **Bit `0x1` = debug UART on** (115200 8N1; see [debug-uart.md](debug-uart.md)). The ToolKit's "sniff enable" checkbox writes bit `0x80000` here, **but 2021 firmware doesn't read it for sniff** | V (bit 0x1: code + hardware); T (rest) |
| `vol_a2dp` / `vol_hfp` / `vol_wave` / `vol_mic` | `0x009`–`0x00C` | u8 | default volume indexes. `vol_wave` affects all prompts | T |
| `lp_channel` | `0x00D` | u8 | battery ADC channel | T |
| `lp_interval` | `0x00E` | u32 | 10 ms ticks (6000 = 60 s) battery-check interval | T |
| `lp_threshold` | `0x012` | u16 | low-battery warning, ≈ mV | V (traced) |
| `lp_pd_threshold` | `0x014` | u16 | low-voltage shutdown, ≈ mV | V (traced) |
| `sleep_timeout` | `0x022` | u16 | 10 ms ticks. Idle time before the sniff/low-power path. Comparison is `if (timeout > counter) return`, so **0 means immediately** | V |
| `powerdown_timeout` | `0x024` | i32 | 10 ms ticks, power off when not connected | T |
| `pause_powerdown_timeout` | `0x039` | i32 | 10 ms ticks. -100 = disabled | T |
| `bt_flag` | `0x059` | u32 | `0x10` auto reconnect, `0x20` link-loss recovery, `0x40` reconnect-address polling, `0x80` spoken numbers, `0x100` periodic auto reconnect | T |
| `disconn_retry_action` | `0x094` | i8 | UI radio + 1 (1 = pairing only, 2 = audio connection) | T |
| `disconn_start` | `0x095` | u32 | ms before recovery starts | T |
| `disconn_retry_count` | `0x099` | i8 | -1 = sentinel (probably unlimited) | T |
| `disconn_retry_interval` | `0x09A` | u32 | ms | T |
| `auto_conn_count` | `0x09E` | i8 | attempts | T |
| `auto_conn_interval` | `0x09F` | u16 | ms (the UI clamps it to ≥ 4000) | T |
| `match_timeout` | `0x0A1` | i16 | pairing timeout s. -1 = sentinel | T |
| `pd_cond` | `0x0A3` | u8 | bit0 no-connection shutdown, bit1 pause shutdown | T |
| `action_disconn` | `0x0A4` | u8 | 0 = wait for pairing, 1 = auto reconnect | T |
| `wave_lang_sel` | `0x0E5` | u8 | prompt bank | T |
| `wave_info[42]` ×5 banks | `0x424` | 3 B each | `type, page:u16`. Bank stride `0x7E` | V (mute written and verified) |
| `led_info[28]` | `0x0E9` | 11 B each | `led u8, on_ms u16, off_ms u16, repeat u8, num_flash u8, timeout u32` (`0xFFFFFFFF` = forever). `led`: 0 LED0, 1 LED1, 2 LED_LP, 3 alternate, 4 both. Printed by `config` | V (layout from the firmware's own `led info[%d]` dump at `0x6D03`; event names inferred from the ToolKit order) |
| `sw_features.feature_flag` | `0x34A` | u32 | **bit `0x80000` gates the sniff path in 2021 firmware** (RAM `r2+0x7C3C`, config RAM base `r2+0x78F2`) | V (code + hardware): clearing it stops the 0x22 disconnect loop |

Example values from one device (a "Cozy" sleep headband, 2021 firmware): warning 3300, shutdown 3100,
`lp_interval` 6000, `sleep_timeout` 3000 (30 s), `disconn_retry_interval` 6000, `disconn_retry_count` -1,
`bt_flag` `0x237`, `feature_flag` `0x001810C7`.

### LED events on the 2021 "Cozy" headband (LED0 = blue, LED1 = red, owner-confirmed)

| Event | Pattern |
|---|---|
| 0 auto-connecting, 1 pairing | blue/red alternating, 200 ms / 200 ms |
| 3 connected, 4 paused, 5 playing, 6–8 calls | blue 0.5 s on / 5 s off |
| 9 low battery | red, 3 flashes then 4 s off |
| 10 charging | red solid |
| 11 battery full | blue solid |
| 12 power on | blue 1.2 s |
| 13 power off | red 1.2 s |
| 20 (not in the 2018 ToolKit list) | red 1 s / 1 s |

ToolKit event order: auto-connecting, match (pairing), no match no conn, connected, pause, play, incoming ringing,
outgoing ringing, call established, low battery, charging, battery full, power on, power off, line in, test mode, FM search.

## Finding the config on other flash sizes

The ToolKit picks the base per flash variant. On a new size: search the logical image for a block where `used` = 1
and the Bluetooth device name sits inside the next `0x77F` bytes, then check that the prompt table at `+0x424`
holds `02 xx xx` records pointing at SBC data (`9C 73 10` sync). Pass that base as `--config-base`.
