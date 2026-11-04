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

The 30 s is the LMP response timeout, not the idle timer. In code, the link policy written at connection is role-switch
only (`0xC2 | 1` at `0x2B1AD`), so sniff is **refused** until the idle path (`0x2654C`, gated by `feature_flag & 0x80000`)
turns policy bit `0x4` on and starts the headset's own sniff entry. Every unanswered request in the capture fell inside that
window. That's consistent with a collision between phone- and headset-initiated sniff. With the bit cleared, the policy stays
"refuse", and refusal is proven to work (the `0x24` above). That makes `--clear-bits feature_flag=0x80000` the first fix to try.

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

**Why not "phone-initiated sniff only"?** It's possible with a code patch: at `0x2B1B4` change `ori r6,r6,0x1` to `0x5` so the
connection policy allows sniff, and clear the feature bit so the headset never initiates it. That's a code-sector write, and
it only helps if the failure needs both sides to initiate sniff together. It's untested.

### Capture hygiene

- Start the PacketLogger trace **before** turning the headset on, so the `Connection Complete` (address, LMP version) is in the trace.
- An Apple Watch paired to the same phone generates hundreds of thousands of L2CAP packets plus its own sniff and disconnect events.
  Its address will show up in `pklg` output (manufacturer `0x004C`). Ignore it, or filter by the headset's handle.
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

## Step 2: bounded firmware experiments (practice board, one change at a time)

| Experiment | Patch | What it tells you |
|---|---|---|
| Move the timer | `--set sleep_timeout=12000` (120 s) | If the first drop moves to ~2 min, the idle path is confirmed as the trigger. |
| Disable sniff | `--clear-bits feature_flag=0x80000` | **Confirmed fix** (see above). Costs some idle battery. On 2021 firmware use `feature_flag`, **not** the ToolKit's `system_flag` checkbox. |
| Tame the loop | `disconn_retry_*`, `auto_conn_*` | Changes the aftermath only. |

Don't set `sleep_timeout` to 0 or `0xFFFF` expecting "disabled". 0 fires immediately, and `0xFFFF` is just ~11 min.
