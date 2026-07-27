# Pigeon rotary encoder

Turns a rotary encoder into Left / Right / Space so Pigeon can navigate without GPIO or Qt code.

Pigeon (Tk) already listens in `pigeonSystem/pigeon_0_8.py`:

| Key | Handler | Effect in main settings |
|-----|---------|-------------------------|
| Left / Right | `on_arrow_remote` / rotary callback | `main_settings_widget.navigate(...)` |
| Space | `on_space_play` / rotary callback | `main_settings_widget.activate()` |

There is **no** `keyPressEvent` — that is a Qt API; this project does not use it.

## Map

- Clockwise → **Right** (forward)
- Counter-clockwise → **Left** (backward / previous)
- Click → **Space** (activate)

If direction feels reversed, swap CLK/DT wires or set `PIGEON_ROTARY_INVERT=1` on the host.

## Transport modes

`rotary_hid.ino` picks a transport at compile time:

| Board class | Transport | Host |
|-------------|-----------|------|
| Leonardo, Pro Micro, Pico, ESP32-S2/S3, … | USB **HID Keyboard** | Built-in key binds only |
| **Arduino UNO Q** (“Arduino Q”), classic Uno UART USB, … | USB **Serial** lines `RIGHT` / `LEFT` / `PUSH` | `pigeon.rotary_serial` drives navigate/activate |

### Arduino UNO Q — use `Monitor`, not `Serial`

On UNO Q, `Serial` is the **UART on pins D0/D1**. It does **not** reach the Raspberry Pi over USB-C.

USB-C / Serial Monitor traffic must use `Monitor` from **Arduino_RouterBridge**. The sketch selects `Monitor` automatically when that library is installed. If you flash without the library, the Pi will never see encoder lines.

### Fix for `'Keyboard' was not declared`

That error means **Tools → Board** is a non-HID board. Either:

- Select a native-USB HID board (Leonardo / Pro Micro / Pico / …), or
- Use **Arduino UNO Q** / serial mode (sketch falls back automatically) and keep Pigeon’s serial bridge enabled.

## Arduino UNO Q

1. Boards Manager: install **Arduino UNO Q Zephyr Core** / Zephyr Boards.  
   If missing, add Additional Boards Manager URL:  
   `https://downloads.arduino.cc/packages/package_zephyr_index.json`
2. Library Manager: install **Arduino_RouterBridge** (and prompted deps).
3. **Tools → Board → Arduino UNO Q**, select the USB-C port, upload `rotary_hid.ino`.
4. Plug the board into the **Raspberry Pi** (or host running Pigeon).
5. Optional: `export PIGEON_ROTARY_PORT=/dev/ttyACM0` if autodetect misses the port.  
   Disable bridge: `PIGEON_ROTARY_SERIAL=0`.  
   On Pi, `pip install pyserial` is recommended (`requirements-pi.txt`).  
   User must be in the `dialout` group: `sudo usermod -aG dialout $USER` (then re-login).

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

1. In Arduino Serial Monitor (USB-C to a laptop), twisting should print `RIGHT` / `LEFT` / `PUSH`.  
   If you see nothing, the sketch is still using UART `Serial` — install RouterBridge and reflash.
2. Start Pigeon on the Pi (update to a build that includes this sketch’s host side).
3. Twist or click — Pigeon opens main settings if needed and navigates.
4. Log line in stderr / `~/.pigeon_0_6/pigeon.log`:  
   `pigeon: rotary_serial: connected …`

## Later: audio on the same Arduino

Keep HID keys (or serial LEFT/RIGHT/PUSH) for the encoder. When you add audio / meters, prefer a second channel or tagged telemetry lines so navigation stays on the existing key path.
