# Hardware hookup

## What you need
- A **3.3 V** USB-UART adapter (FTDI TTL-232R-3V3 or similar). **5 V logic can damage the chip.**
- Fine wire (30–32 AWG), a fine-tip iron, flux.
- Optional: a 330 Ω–1 kΩ resistor in series with the adapter's TX line.
- Python 3.9+ and `pip install pyserial`.

## Finding UART pads
Many BK3266 boards have two small unlabeled round test pads next to the chip, which carry chip UART TX and RX.
On the `JXD_2115 3K_V2` board these sit just left of the chip on the component side: the **lower pad is chip TX** and the **upper pad is chip RX**.

On other boards:
1. With the board powered from its battery, measure the candidate pads to ground. Both idle near 3.3 V. TX is driven and RX is usually pulled up.
2. Wire the adapter's RX to your best guess for chip TX, adapter TX to chip RX, and GND to battery negative or board ground.
3. Run `bk3266.py ping` and press power. If nothing happens after a few tries, swap TX and RX. That's harmless at 3.3 V with a series resistor.

**Don't connect the adapter's VCC/red wire.** Power the board from its own LiPo (or bench supply on B+/B−).
The application firmware normally prints nothing on UART, so a silent line is expected until the bootloader answers.

## Catching the bootloader
The ROM bootloader listens briefly at power-on. The host must already be sending LinkCheck when the owner
**presses and holds the power button**. It can take a few presses. Once `ping` says connected, the button can be released.

## Practice discipline
- Use a spare board for anything that writes. Keep two matching full backups per board (`dump`).
- Every board has its own pairing and calibration records (`0x7F000`). Don't write one board's full image to another;
  write only the sectors you changed.
