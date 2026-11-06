# Diagnosing BK3266 Bluetooth drops

## The common pattern: drops ~N seconds after audio stops, then a connect/disconnect loop

On the firmware analysed so far:

1. A2DP stream start resets an idle counter and **disables sniff**.
2. When audio stops, the counter runs. Once it reaches `sleep_timeout` (often 3000 = 30 s), the idle path asks
   the controller to **enter sniff mode** (320–400 ms interval), and the chip may deep-sleep between sniff anchors.
3. Nothing in that path asks for a disconnect. If the link drops, it's because sniff negotiation or low-power timing failed.
   Typical causes: the phone rejects or mis-handles the sniff parameters, or the chip oversleeps, misses anchors and hits
   supervision timeout.
4. Link-loss recovery and auto-reconnect (`bt_flag` `0x20`/`0x10`, unlimited retries, ~4–6 s intervals) reconnect it.
   The idle timer then fires again, so you get a loop of "disconnected… connected…" prompts.

**Test it, don't assume it.** First measure the time from pause to first drop, then capture the reason code.

### What a real capture showed (iPhone + 2021 "Cozy" headband, 2026-11-03)

The headset reports LMP version `0x09` (BT 5.0), manufacturer `0x000E`, subversion `0x1450`. The drop is **not** an oversleep:

```
09:29:38  CMD  Sniff Mode handle 0x29 max 240ms min 240ms attempt 1 timeout 1   ← iPhone asks for sniff
          (no Mode Change: the headset never answers)
09:30:08  EVT  Disconnection Complete handle 0x29 reason 0x22 LMP Response Timeout   ← exactly 30 s later
09:30:08  EVT  Connection Complete ...                                              ← auto-reconnect
09:30:42  CMD  Sniff Mode ...  →  Mode Change status 0x24 (LMP PDU not allowed)     ← clean refusal, no drop
09:31:22  EVT  Mode Change -> sniff 320ms; 100 ms later -> active                   ← headset's own sniff (0x200 slots)
09:31:37  CMD  Sniff Mode ...   (no answer)
09:32:07  EVT  Disconnection Complete reason 0x22 LMP Response Timeout
```

The 30 s is the LMP response timeout, not the idle timer (the matching `sleep_timeout` of 30 s is a coincidence). In code, the link policy written at connection is role-switch
only (`0xC2 | 1` at `0x2B1AD`), so sniff is **refused** until the idle path (`0x2654C`, gated by `feature_flag & 0x80000`)
turns policy bit `0x4` on and starts the headset's own sniff entry. Every unanswered request in the capture fell inside that
window. (We first suspected a collision between phone- and headset-initiated sniff; the headset's own log, below, ruled
that out.) With the bit cleared, the policy stays "refuse", and refusal is proven to work (the `0x24` above). That makes
`--clear-bits feature_flag=0x80000` the first fix to try.

### Fix confirmed: clear `feature_flag` bit `0x80000` (same board, same phone, 2026-11-03)

A 3-byte write: config `+0x34A` `0x001810C7` → `0x001010C7`, raw `0x7C880` `18`→`10`, plus that record's CRC. The capture afterwards:

```
11:50:32  CMD  Sniff Mode handle 0x15 max 240ms min 240ms attempt 1 timeout 1
11:50:32  EVT  Mode Change handle 0x15 -> active status 0x24      ← refused in 12 ms
          (20 minutes idle: no further sniff requests, no headset-initiated sniff, no disconnects)
12:10:42  EVT  Disconnection Complete reason 0x13                 ← owner powered the headset off
```

After one clean refusal, iOS stops asking and keeps the link active. The cost is idle current while connected and silent.
During A2DP streaming the firmware refuses sniff anyway, so all-night audio is unaffected. A multi-night battery
comparison is still pending.

### Fixed properly: the headset now accepts the phone's sniff (2026-11-04)

The bug is in the controller's LMP_sniff_req handler (logical `0x3D217`, LMP dispatch table entry 23 at `0x56880 + 23*4`).
It only accepts sniff intervals of 320–400 ms (`0x200`–`0x280` slots) and attempt ≥ 4. For anything else it
**counter-proposes, but clamps the interval to a placeholder minimum of 4 slots** (`0x3D3CF`), giving `Tsniff=4, attempt=4`,
which breaks the spec's `attempt ≤ Tsniff/2` rule. The iPhone rejects that with `0x1E`, and its own request is never answered,
so 30 s later the link drops with `0x22`.

Apple's Accessory Design Guidelines (§56.3) say accessories shall "accept sniff mode requests and support valid parameters"
and, when the Apple device requests sniff, "accept the request and parameters without negotiation". The Bluetooth spec
allows three responses: accept, a *valid* counter-proposal (LMP_sniff_req with new parameters), or LMP_not_accepted.

**Final patch set (2021 "Cozy" build).** The handler now does this: accept anything from 15–400 ms with attempt ≥ 1; otherwise
counter-propose a valid interval clamped into that window; after 4 rounds, refuse cleanly (`0x20`, stock code).

| Patch | Logical | Old → new | Effect |
|---|---|---|---|
| B | `0x3D2A8` | `d1 f0 5b 00` → `d1 c0 5b 00` (`bgtui r5,0x3` → `0x0`) | accept sniff attempt ≥ 1 (was: ≤ 3 forced to 4, then negotiation) |
| A | `0x3D3AD`–`0x3D3D3` (39 bytes) | `28eab328cad3d38c790028ea53016028cad3d8c6007f3e06329f460100000047210028ea530160` → `28cad3d8e018003de73064e732d8a001403de72864e53a28ea533d4730656b0238a50000100010` | replaces "raise to link minimum" plus the buggy window check with one clamp: `T' = min(max(T, 0x18), 0x280)`; if `T' ≠ T`, mark "needs counter-proposal". Same size, nothing else moves |

The A block, as assembled:
```
3d3ad  lhz  r6,0xca(r10)      ; T = requested Tsniff
3d3b0  addi r7,r0,0x18        ; LO = 24 slots (15 ms)
3d3b4  sfgtu r7,r6 / cmov r7,r7,r6     ; r7 = max(T, LO)
3d3ba  addi r5,r0,0x280       ; HI = 640 slots (400 ms)
3d3be  sfgtu r7,r5 / cmov r7,r5,r7     ; r7 = min(r7, HI)
3d3c4  sh   0xca(r10),r7      ; store
3d3c7  sfeq r7,r6 / cmov r11,r11,r0    ; changed → acceptable = 0 → counter-propose r7
3d3cd  ori r5,r5,0 ; nop ; nop          ; padding to 0x3D3D4
```
It uses only instructions whose meaning is confirmed by the handler's own code (`sfgtu`+`cmov` at `0x3D39B`/`0x3D3A1`).
pyba2 marks `bn.max`/`bn.min` as unknown, so they're avoided. Design choice: the link's own minimum (`0xCC`) is no longer applied here,
because the window's LO covers it. The link-maximum clamp just before it is unchanged.

**Results, 2026-11-04, iPhone, stock config with sniff on:**

| Firmware | What the iPhone's 240 ms / attempt 1 request got | Link |
|---|---|---|
| stock | invalid counter-proposal (4 slots) → phone rejects `0x1E`, its own request hangs | drops after 30 s (`0x22`), loop |
| B + C (accept / refuse, intermediate) | **accepted in 13 ms**, sniff 567 of 608 s (93%), one 9½-min stretch | stable, play resumes in 150 ms |
| A only, stock 320–400 ms window (test) | **valid** counter-proposal 320 ms / attempt 4 → iOS declines with `0x20` in 9 ms, transaction closes cleanly | stable. The headset's own 320 ms sniff still works. iOS doesn't take counter-proposals |
| **A + B (final)** | **accepted in 14 ms**, every time (5/5); sniff 289 of 392 s (74%), one 4 min 22 s stretch | stable, play resumes immediately |

Short sniff periods early in a run end at the radio layer, with no command from either host, so they aren't firmware-driven.
For an accepted request, B+C and A+B execute identical code.

Not yet exercised: the 4-round give-up path (an iPhone never counter-proposes back), battery comparison
against the config-only fix, and multi-night use.

**Android (Pixel 5, Android 14, A+B build, 2026-11-06): stable, no idle drops in ~35 min.** `bk3266.py pklg` reads Android's
`btsnoop_hci.log` too (Developer options → Enable Bluetooth HCI snoop log → Enabled, restart Bluetooth, then
`adb bugreport` and take `FS/data/misc/bluetooth/logs/btsnoop_hci.log` from the zip).
- Android asks for sniff 250–500 ms, attempt 4, timeout 1, repeatedly (every 10–60 s) and doesn't give up after a refusal.
  Before the headset's own idle `sniff enable` it gets `0x24` (fine); after it, the patched headset accepts at **400 ms**.
  Headset-initiated 320 ms sniff is also accepted. Both directions held for minutes.
- With the iPhone and the Pixel both connected, both links reach sniff (`sniff enable:0` / `enter sniff:1`, then `enter sniff:0`).
- **Seen once, not reproduced:** after a boot where the iPhone auto-connected and the Pixel was then *paired for the first time*,
  the Pixel's link spent 17 min refusing every sniff (`0x24`) while the headset logged only `enter sniff:1,0033` every 30 s,
  never `sniff enable`. The idle routine (`0x2FFxx`, calling the policy setter `0x2654C`) skips the enable when its per-link
  "policy on" byte is already set, so that byte said "on" while the controller's link policy was off. No drops, just no
  power saving. A headset power cycle cleared it. Suspect: a stale per-link flag on the first-pairing path.
- When a second phone connects, the headset drops the first one (headset log: reason `0x16`; phone: `0x13`) and
  reconnects it within about 1 s. That's a multipoint quirk, not the sniff bug.

### Root cause, from the headset's own log (2026-11-04)

With the [debug UART](debug-uart.md) on and the Mode Change log line widened to print the status, the phone trace and
the headset log line up to the second (sniff re-enabled, i.e. stock behaviour):

| Time | Phone (PacketLogger) | Headset (debug UART) |
|---|---|---|
| 14:16:10 | Sniff Mode 240 ms → **refused `0x24`**, link fine | (nothing: policy still "refuse") |
| 14:16:25 | | `sniff enable` (idle timer: policy now allows sniff) |
| 14:16:55 | Mode Change → sniff 320 ms | `enter sniff` → `ep.unit_mode=2003200` (mode 2, status 00): **headset-initiated sniff works** |
| 14:17:02 | Mode Change → active | `ep.unit_mode=3200` |
| 14:17:17 | **Sniff Mode 240 ms, attempt 1, timeout 1** | `ep.unit_mode=321e`: mode 0, **status `0x1E` Invalid LMP Parameters**, same second |
| 14:17:32 | | `enter sniff` (own retry), no result |
| 14:17:47 | Disconnection Complete `0x22` | `disconn_compl … 0x22` |

**When its policy allows sniff, the headset's link manager answers the iPhone's request with an invalid counter-proposal
(see the section above). The iPhone rejects it with `0x1E`, and the headset passes that up to its own host.** The iPhone's own request is never
answered, so it waits out the 30 s LMP response timeout and drops the link. The headset's own retries go nowhere in the meantime. Refusal (`0x24`) is
answered properly, and sniff that the headset initiates (320–400 ms, attempt 4, timeout 4) works in both directions.
The bug is in the controller's handling of a *remote* sniff request it considers invalid. The host stack only logs the
symptom. The first connection of the earlier capture showed that the headset doesn't need to have initiated sniff for this to happen.

### Experiments, and what each one ruled out

| Patch | Result | Verdict |
|---|---|---|
| Clear `feature_flag` `0x80000` | Policy never allows sniff; phone gets `0x24` once, stops asking; no drops (20 min) | **Fix.** No idle sniff. |
| Stock + debug UART | `0x1E` on phone-initiated sniff, then `0x22` | Root cause (above) |
| Policy never allows sniff, but headset still initiates its own (`0x265B1` `ori r7,r7,0x4` → `0x0`, `--poke 0x265b1=38e720:38e700`) | Stable (6.5 min, one link), phone refused once. **But all 10 headset `enter sniff` attempts were silently rejected**: no Mode Change | The controller enforces local link policy on locally initiated sniff too. Same outcome as the fix. |
| Moving `sleep_timeout` (e.g. 45 s or 20 s) | Not run | Only moves when the "allow" window opens. A shorter timer makes it worse. |
| Accept the phone's parameters (B) + never counter-propose (C), above | Sniff at 240 ms for 9½ min, no drops, resumes audio in 150 ms | **Proper fix**: low power and a stable link |

The link manager runs from flash (it sets LMP version 9 / subversion `0x1450` / manufacturer `0x000E` at `0x3C517`), so it can be patched.
Its LMP handlers are reached through a table of function pointers indexed by opcode (logical `0x56880`; `0x32313` = unsupported).
The dispatcher (`0xA3AA`) sends LMP_not_accepted with whatever non-zero status a handler returns. The HCI Sniff Mode handler
(`0x3CB4F`) enforces the local link policy (`0x1D4(link) & 0x4`, else `0x0C`), which is why "refuse the phone, sniff ourselves" failed.

### Capture hygiene

- **A phone capture contains every Bluetooth device the phone talks to**: watch, earbuds, car, keyboard, Mac. Their sniff
  switches and disconnects look just like the bug. Identify the headset first, then filter:
  1. `pklg capture.pklg` ends with a **Devices seen** table: address, LMP version, manufacturer, connection count and disconnect
     reasons. BK3266 parts report LMP `0x09`, manufacturer `0x000E` (Ceva baseband IP). Apple devices report `0x004C`.
  2. `pklg capture.pklg --device <headset address>` shows only that device's events, across reconnects (the handle number changes each time).
  3. If the headset connected before the trace started, its address isn't in the trace. The summary lists such links by
     handle; use `--handle 0x15`.
- Start the PacketLogger trace **before** turning the headset on, so its `Connection Complete` (address, version) is captured.
- Example: on an iPhone, a paired Apple Watch produced hundreds of thousands of packets and 25 disconnect/reconnect cycles
  during a 20-minute capture, all unrelated to the headset.
- PacketLogger autosaves the live trace to `~/<date> <time>.pklg`. macOS puts a narrow no-break space before "PM" in the
  name, so copy it with `find ~ -maxdepth 1 -name '*.pklg'` rather than typing the name.

## Step 1: capture the HCI disconnect reason (no firmware change)

### iPhone
1. On the phone, in Safari, go to developer.apple.com/bug-reporting/profiles-and-logs/, sign in with any Apple ID, and download the **Bluetooth** iOS profile.
2. Go to Settings → *Profile Downloaded* (or General → VPN & Device Management) → Install, then **restart the phone**.
3. **Live capture (recommended):** install *Additional Tools for Xcode* on a Mac (developer.apple.com/download/all) and open
   **PacketLogger** (in the Hardware folder). Connect the iPhone by cable, trust the computer, then choose File → New iOS Trace.
   Save the result as `.pklg`.
4. **Or use sysdiagnose:** reproduce the drop, then press and hold both volume buttons and the side button for about 1 s (until a short vibration).
   After ~10 min, go to Settings → Privacy & Security → Analytics & Improvements → Analytics Data → `sysdiagnose_…`.
   AirDrop it to a Mac and find the `*.pklg` files under its Bluetooth logs.
5. The profile expires after a few days.

### Android
Go to Developer options → **Enable Bluetooth HCI snoop log**, then toggle Bluetooth off and on. Reproduce the drop, then run `adb bugreport`.
The `btsnoop_hci.log` inside opens in Wireshark (filter `bthci_evt.code == 0x05 || bthci_evt.code == 0x14`).

### Read it
```sh
python3 scripts/bk3266.py pklg capture.pklg
```
This prints sniff requests, Mode Change events (active ↔ sniff, with interval), failed command statuses and
**Disconnection Complete with its reason**.

| Reason | Meaning | Points to |
|---|---|---|
| `0x08` | Connection (supervision) timeout | Headset missed sniff anchors or the radio dropped out. Low-power timing. |
| `0x22` | LMP response timeout | Headset stopped answering LMP, often while asleep. |
| `0x13` | Remote user terminated | The *other* side chose to disconnect (from the phone's log, that's the headset). |
| `0x16` | Terminated by local host | The side that logged it chose to disconnect (from the phone's log, that's the phone's own decision). |
| `0x1A` | Unsupported remote feature | Sniff or feature negotiation mismatch. |

## Step 1b: read the headset's side (debug UART)

The phone's capture only shows what the phone saw. Turn on the headset's own log first ([debug-uart.md](debug-uart.md)):
it's a 3-byte config write that's easy to undo, and it shows every sniff, mode change and disconnect from the inside.

## Step 2: bounded firmware experiments (practice board, one change at a time)

| Experiment | Patch | What it tells you |
|---|---|---|
| Move the timer | `--set sleep_timeout=12000` (120 s) | If the first drop moves to ~2 min, the idle path is confirmed as the trigger. |
| Disable sniff | `--clear-bits feature_flag=0x80000` | **Confirmed fix** (see above). Costs some idle battery. On 2021 firmware use `feature_flag`, **not** the ToolKit's `system_flag` checkbox. |
| Tame the loop | `disconn_retry_*`, `auto_conn_*` | Changes the aftermath only. |

Don't set `sleep_timeout` to 0 or `0xFFFF` expecting "disabled". 0 fires immediately, and `0xFFFF` is just ~11 min.
