# Pigeon rotary (USB HID keyboard)

Turns a rotary encoder on a **native-USB Arduino** into Left / Right / Space so Pigeon can navigate without any GPIO or Qt code.

Pigeon (Tk) already listens in `pigeonSystem/pigeon_0_8.py`:

| Key | Handler | Effect in main settings |
|-----|---------|-------------------------|
| Left / Right | `on_arrow_remote` | `main_settings_widget.navigate(...)` |
| Space | `on_space_play` | `main_settings_widget.activate()` |

There is **no** `keyPressEvent` — that is a Qt API; this project does not use it.

## Requirements

- Board with **USB HID Keyboard** support: Leonardo, Pro Micro, Pico (Arduino-Pico), ESP32-S2/S3 / Nano ESP32, etc.
- A classic **Uno / Nano / Mega** (UART-only USB) will **not** compile or enumerate as a keyboard.

### Fix for `'Keyboard' was not declared`

That error means **Tools → Board** is set to a non-HID board (often Uno). Change it to match your hardware, for example:

| Hardware | Tools → Board |
|----------|----------------|
| Arduino Leonardo / Micro | Arduino Leonardo / Arduino Micro |
| Pro Micro (SparkFun / clone) | SparkFun Pro Micro, or **Arduino Leonardo** |
| Pico | Raspberry Pi Pico (Earle Philhower or Arduino Mbed) |
| Nano ESP32 / ESP32-S3 | Arduino Nano ESP32 / your ESP32-S3 board |

Then upload again.

## Wiring (defaults in `rotary_hid.ino`)

| Encoder | Arduino pin |
|---------|-------------|
| CLK / A | 2 |
| DT / B  | 3 |
| SW      | 4 |
| GND     | GND |
| +       | 5V or 3V3 (match your module) |

Change `PIN_CLK` / `PIN_DT` / `PIN_SW` in the sketch if your wiring differs.

## Flash

1. Open `rotary_hid.ino` in Arduino IDE.
2. Select your board + port.
3. Upload.
4. Plug the board into the **Raspberry Pi USB** port (same cable is fine after upload).

## Map

- Clockwise → **Right**
- Counter-clockwise → **Left**
- Click → **Space**

If direction feels reversed, swap CLK/DT wires or invert the DT check in the sketch.

## Verify

1. Start Pigeon on the Pi.
2. Open main settings (Tab).
3. Twist the encoder — focus should move Left/Right.
4. Click — activate the focused control.

## Later: audio on the same Arduino

Keep this HID path for the encoder. When you add audio / meters, prefer **USB Serial** on the same device (`Serial.begin(...)` alongside `Keyboard.begin()`), then have Pigeon read that port separately. Encoder keys stay Left/Right/Space; telemetry does not need to go through the keyboard.
