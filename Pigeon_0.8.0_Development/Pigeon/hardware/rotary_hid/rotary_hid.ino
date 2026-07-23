/*
  Pigeon rotary → USB HID keyboard

  Plug this board into the Raspberry Pi USB port. Pigeon already binds:
    Left / Right  → settings navigate (on_arrow_remote in pigeon_0_8.py)
    Space         → settings activate  (on_space_play)

  Map:
    CW  → Right
    CCW → Left
    click → Space

  Tools → Board MUST be a native-USB HID board, for example:
    - Arduino Leonardo / Micro
    - SparkFun Pro Micro (or Leonardo if using a clone)
    - Arduino Nano ESP32 / ESP32-S2 / ESP32-S3 (USB OTG)
    - Raspberry Pi Pico (Arduino-Pico core with Keyboard)

  Classic Uno / Nano / Mega (UART USB chip only) cannot compile Keyboard —
  that is why you see "'Keyboard' was not declared".

  Default wiring (KY-040-style module, shared GND/+5V or 3V3):
    CLK / A  → pin 2
    DT  / B  → pin 3
    SW       → pin 4  (INPUT_PULLUP)
*/

// --- Keyboard API by core -------------------------------------------------
#if defined(ARDUINO_ARCH_ESP32)
  // ESP32-S2 / S3 / Nano ESP32: USB device HID
  #include "USB.h"
  #include "USBHIDKeyboard.h"
  static USBHIDKeyboard Keyboard;
  #ifndef KEY_LEFT_ARROW
    #define KEY_LEFT_ARROW 0xD8
  #endif
  #ifndef KEY_RIGHT_ARROW
    #define KEY_RIGHT_ARROW 0xD7
  #endif
#elif defined(USBCON) || defined(ARDUINO_ARCH_RP2040) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_MBED)
  #include <Keyboard.h>
#else
  #error "Pigeon rotary_hid needs a native-USB board (Leonardo, Pro Micro, Pico, ESP32-S2/S3). Tools → Board: do not select classic Uno/Nano/Mega."
#endif

// --- Pins (change to match your wiring) ---
static const uint8_t PIN_CLK = 2;
static const uint8_t PIN_DT = 3;
static const uint8_t PIN_SW = 4;

// Ignore encoder edges closer than this (ms) to cut bounce / half-step chatter.
static const unsigned long ENCODER_MIN_MS = 8;
// Coalesce rapid turns: at most one key every this many ms while spinning.
static const unsigned long TURN_COOLDOWN_MS = 40;
// Button debounce and minimum Space gap (Pigeon also debounces Space ~120ms).
static const unsigned long BUTTON_DEBOUNCE_MS = 30;
static const unsigned long SPACE_MIN_MS = 150;

static uint8_t lastClk = HIGH;
static unsigned long lastEdgeMs = 0;
static unsigned long lastTurnMs = 0;
static unsigned long lastSpaceMs = 0;

static uint8_t lastSwRaw = HIGH;
static uint8_t swStable = HIGH;
static unsigned long swChangeMs = 0;

static void tapKey(uint8_t key) {
  Keyboard.press(key);
  delay(8);
  Keyboard.release(key);
}

void setup() {
  pinMode(PIN_CLK, INPUT_PULLUP);
  pinMode(PIN_DT, INPUT_PULLUP);
  pinMode(PIN_SW, INPUT_PULLUP);
  lastClk = digitalRead(PIN_CLK);
  lastSwRaw = digitalRead(PIN_SW);
  swStable = lastSwRaw;

#if defined(ARDUINO_ARCH_ESP32)
  USB.begin();
#endif
  Keyboard.begin();
  // Optional later: Serial.begin(115200) for audio / telemetry on the same USB device.
}

void loop() {
  const unsigned long now = millis();

  // --- Rotary (count on CLK falling edge; DT selects direction) ---
  const uint8_t clk = digitalRead(PIN_CLK);
  if (clk != lastClk) {
    if (clk == LOW && (now - lastEdgeMs) >= ENCODER_MIN_MS) {
      lastEdgeMs = now;
      if ((now - lastTurnMs) >= TURN_COOLDOWN_MS) {
        lastTurnMs = now;
        if (digitalRead(PIN_DT) == HIGH) {
          tapKey(KEY_RIGHT_ARROW);  // CW
        } else {
          tapKey(KEY_LEFT_ARROW);   // CCW
        }
      }
    }
    lastClk = clk;
  }

  // --- Push button → Space ---
  const uint8_t sw = digitalRead(PIN_SW);
  if (sw != lastSwRaw) {
    lastSwRaw = sw;
    swChangeMs = now;
  }
  if ((now - swChangeMs) >= BUTTON_DEBOUNCE_MS && sw != swStable) {
    swStable = sw;
    // Active-low switch (common on KY-040): press when going LOW.
    if (swStable == LOW && (now - lastSpaceMs) >= SPACE_MIN_MS) {
      lastSpaceMs = now;
      tapKey(' ');
    }
  }
}
