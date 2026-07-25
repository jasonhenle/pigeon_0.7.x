/*
  Pigeon rotary encoder → host navigation

  Intended map (matches pigeon_0_8.py hotkeys):
    CW   → forward  (Right / serial RIGHT)
    CCW  → backward (Left  / serial LEFT)   — not a "password" action
    click → activate (Space / serial PRESS)

  Two transport modes (picked at compile time):

  1) USB HID Keyboard — Leonardo, Pro Micro, Pico, ESP32-S2/S3, …
     Emits real Left / Right / Space. No host serial code needed.

  2) USB Serial line protocol — Arduino UNO Q (STM32 MCU / Zephyr) and
     other boards without Keyboard HID.
     Emits lines: RIGHT / LEFT / PRESS (+ PIGEON_CONTROLLER_READY).
     Host: pigeon.rotary_serial in pigeon_0_8.py synthesizes the same keys.

  Arduino UNO Q ("Arduino Q"):
    Tools → Board → Arduino UNO Q (Arduino UNO Q Zephyr Core / Zephyr Boards).
    Boards Manager URL if missing:
      https://downloads.arduino.cc/packages/package_zephyr_index.json
    Install Library Manager: Arduino_RouterBridge (+ deps) for Serial over USB-C.
    On Zephyr core ≥ 0.55, Serial is the USB/bridge monitor (not D0/D1 UART).
    Pins below are UNO-header D2/D3/D4 (STM32 GPIOs on UNO Q).

  Default wiring (KY-040-style module, shared GND / 5V or 3V3):
    CLK / A  → pin 2
    DT  / B  → pin 3
    SW       → pin 4  (INPUT_PULLUP)

  Force serial mode on an HID-capable board:  -DPIGEON_FORCE_SERIAL
  Force HID when detection is wrong:          -DPIGEON_FORCE_HID
*/

// --- Transport selection ----------------------------------------------------
#if defined(PIGEON_FORCE_SERIAL)
  #define PIGEON_USE_SERIAL 1
#elif defined(PIGEON_FORCE_HID)
  #define PIGEON_USE_SERIAL 0
#elif defined(ARDUINO_ARCH_ESP32)
  #define PIGEON_USE_SERIAL 0
#elif defined(USBCON) || defined(ARDUINO_ARCH_RP2040) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_MBED)
  #define PIGEON_USE_SERIAL 0
#else
  // UNO Q (Zephyr), classic Uno/Nano/Mega UART USB, etc.
  #define PIGEON_USE_SERIAL 1
#endif

#if !PIGEON_USE_SERIAL
  #if defined(ARDUINO_ARCH_ESP32)
    #include "USB.h"
    #include "USBHIDKeyboard.h"
    static USBHIDKeyboard Keyboard;
    #ifndef KEY_LEFT_ARROW
      #define KEY_LEFT_ARROW 0xD8
    #endif
    #ifndef KEY_RIGHT_ARROW
      #define KEY_RIGHT_ARROW 0xD7
    #endif
  #else
    #include <Keyboard.h>
  #endif
#endif

// --- Pins (change to match your wiring) ---
static const uint8_t PIN_CLK = 2;
static const uint8_t PIN_DT = 3;
static const uint8_t PIN_SW = 4;

// Ignore encoder edges closer than this (ms) to cut bounce / half-step chatter.
static const unsigned long ENCODER_MIN_MS = 8;
// Coalesce rapid turns: at most one step every this many ms while spinning.
static const unsigned long TURN_COOLDOWN_MS = 40;
// Button debounce and minimum activate gap (Pigeon also debounces Space ~120ms).
static const unsigned long BUTTON_DEBOUNCE_MS = 30;
static const unsigned long ACTIVATE_MIN_MS = 150;

static uint8_t lastClk = HIGH;
static unsigned long lastEdgeMs = 0;
static unsigned long lastTurnMs = 0;
static unsigned long lastActivateMs = 0;

static uint8_t lastSwRaw = HIGH;
static uint8_t swStable = HIGH;
static unsigned long swChangeMs = 0;

#if !PIGEON_USE_SERIAL
static void tapKey(uint8_t key) {
  Keyboard.press(key);
  delay(8);
  Keyboard.release(key);
}
#endif

static void emitForward() {
#if PIGEON_USE_SERIAL
  Serial.println(F("RIGHT"));
#else
  tapKey(KEY_RIGHT_ARROW);
#endif
}

static void emitBackward() {
#if PIGEON_USE_SERIAL
  Serial.println(F("LEFT"));
#else
  tapKey(KEY_LEFT_ARROW);
#endif
}

static void emitActivate() {
#if PIGEON_USE_SERIAL
  Serial.println(F("PRESS"));
#else
  tapKey(' ');
#endif
}

void setup() {
  pinMode(PIN_CLK, INPUT_PULLUP);
  pinMode(PIN_DT, INPUT_PULLUP);
  pinMode(PIN_SW, INPUT_PULLUP);
  lastClk = digitalRead(PIN_CLK);
  lastSwRaw = digitalRead(PIN_SW);
  swStable = lastSwRaw;

#if PIGEON_USE_SERIAL
  Serial.begin(115200);
  // UNO Q bridge Serial may not block; repeat READY so host autodetect can catch it.
  delay(100);
  Serial.println(F("PIGEON_CONTROLLER_READY"));
  delay(100);
  Serial.println(F("PIGEON_CONTROLLER_READY"));
#else
  #if defined(ARDUINO_ARCH_ESP32)
    USB.begin();
  #endif
  Keyboard.begin();
  // Optional later: Serial.begin(115200) for audio / telemetry on the same USB device.
#endif
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
          emitForward();   // CW
        } else {
          emitBackward();  // CCW → Left / backward (not password)
        }
      }
    }
    lastClk = clk;
  }

  // --- Push button → activate (Space) ---
  const uint8_t sw = digitalRead(PIN_SW);
  if (sw != lastSwRaw) {
    lastSwRaw = sw;
    swChangeMs = now;
  }
  if ((now - swChangeMs) >= BUTTON_DEBOUNCE_MS && sw != swStable) {
    swStable = sw;
    // Active-low switch (common on KY-040): press when going LOW.
    if (swStable == LOW && (now - lastActivateMs) >= ACTIVATE_MIN_MS) {
      lastActivateMs = now;
      emitActivate();
    }
  }
}
