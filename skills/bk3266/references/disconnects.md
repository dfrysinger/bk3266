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

**When its policy allows sniff, the headset's link manager judges the iPhone's sniff parameters invalid, reports
`0x1E` to its own host, and never sends LMP_not_accepted back over the air.** The phone waits out the 30 s LMP response
timeout and drops the link, and the headset's own retries go nowhere in the meantime. Refusal (`0x24`) is
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

Paths still open toward "sniff with no drops": toggle the policy around the headset's own sniff entry (allow → Sniff Mode
→ refuse once in sniff), or fix the link manager so that it answers LMP_not_accepted (or accepts 240 ms). Both are code patches.
Find the link manager's sniff_req handler by searching for where it produces `0x1E`.

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
