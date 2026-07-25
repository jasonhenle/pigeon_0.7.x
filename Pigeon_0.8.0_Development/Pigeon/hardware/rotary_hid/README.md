# Pigeon rotary encoder

Turns a rotary encoder into Left / Right / Space so Pigeon can navigate without GPIO or Qt code.

Pigeon (Tk) already listens in `pigeonSystem/pigeon_0_8.py`:

| Key | Handler | Effect in main settings |
|-----|---------|-------------------------|
| Left / Right | `on_arrow_remote` | `main_settings_widget.navigate(...)` |
| Space | `on_space_play` | `main_settings_widget.activate()` |

There is **no** `keyPressEvent` — that is a Qt API; this project does not use it.

## Map

- Clockwise → **Right** (forward)
- Counter-clockwise → **Left** (backward / previous — not Wi‑Fi “password”)
- Click → **Space** (activate)

If direction feels reversed, swap CLK/DT wires or invert the DT check in the sketch.

## Transport modes

`rotary_hid.ino` picks a transport at compile time:

| Board class | Transport | Host |
|-------------|-----------|------|
| Leonardo, Pro Micro, Pico, ESP32-S2/S3, … | USB **HID Keyboard** | Built-in key binds only |
| **Arduino UNO Q** (“Arduino Q”), classic Uno UART USB, … | USB **Serial** lines `RIGHT` / `LEFT` / `PRESS` | `pigeon.rotary_serial` synthesizes the same keys |

### Fix for `'Keyboard' was not declared`

That error means **Tools → Board** is a non-HID board. Either:

- Select a native-USB HID board (Leonardo / Pro Micro / Pico / …), or
- Use **Arduino UNO Q** / serial mode (sketch falls back automatically) and keep Pigeon’s serial bridge enabled.

## Arduino UNO Q

1. Boards Manager: install **Arduino UNO Q Zephyr Core** / Zephyr Boards.  
   If missing, add Additional Boards Manager URL:  
   `https://downloads.arduino.cc/packages/package_zephyr_index.json`
2. Library Manager: install **Arduino_RouterBridge** (and prompted deps). On Zephyr core ≥ 0.55, `Serial` is the USB-C bridge monitor.
3. **Tools → Board → Arduino UNO Q**, select the USB-C port, upload `rotary_hid.ino`.
4. Plug the board into the **Raspberry Pi** (or host running Pigeon).
5. Optional: `export PIGEON_ROTARY_PORT=/dev/ttyACM0` if autodetect misses the port.  
   Disable bridge: `PIGEON_ROTARY_SERIAL=0`.  
   On Pi, `pip install pyserial` is recommended (`requirements-pi.txt`).

## Wiring (defaults in `rotary_hid.ino`)

| Encoder | Arduino pin |
|---------|-------------|
| CLK / A | 2 |
| DT / B  | 3 |
| SW      | 4 |
| GND     | GND |
| +       | 5V or 3V3 (match your module) |

Change `PIN_CLK` / `PIN_DT` / `PIN_SW` in the sketch if your wiring differs. On UNO Q these are the classic UNO header D2/D3/D4 GPIOs.

## Flash (HID boards)

1. Open `rotary_hid.ino` in Arduino IDE.
2. Select board + port (Leonardo / Pro Micro / Pico / …).
3. Upload.
4. Plug into the Pi USB port.

## Verify

1. Start Pigeon on the host.
2. Open main settings (Tab).
3. Twist the encoder — focus should move Left/Right.
4. Click — activate the focused control.

For serial mode, stderr / `~/.pigeon_0_6/pigeon.log` should show  
`pigeon: rotary_serial: connected …` when the port is claimed.

## Later: audio on the same Arduino

Keep HID keys (or serial LEFT/RIGHT/PRESS) for the encoder. When you add audio / meters, prefer a second Serial channel or tagged telemetry lines so navigation stays on the existing key path.
