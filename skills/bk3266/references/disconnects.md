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
| Disable sniff | `--clear-bits feature_flag=0x80000` | Likely real fix. Costs some idle battery. On 2021 firmware use `feature_flag`, **not** the ToolKit's `system_flag` checkbox. |
| Tame the loop | `disconn_retry_*`, `auto_conn_*` | Changes the aftermath only. |

Don't set `sleep_timeout` to 0 or `0xFFFF` expecting "disabled". 0 fires immediately, and `0xFFFF` is just ~11 min.
