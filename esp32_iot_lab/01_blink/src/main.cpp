// Step 01 — prove the pipeline: compile -> flash -> run -> read the log.
// Nothing wired to the board: it blinks the on-board LED (GPIO2) and prints
// a heartbeat on Serial.

#include <Arduino.h>

constexpr uint8_t LED_PIN = 2;             // on-board blue LED on DevKit V1
constexpr unsigned long BLINK_MS = 500;
constexpr unsigned long HEARTBEAT_MS = 1000;

unsigned long lastBlink = 0;
unsigned long lastHeartbeat = 0;
bool ledOn = false;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  delay(200);  // let the USB-serial bridge settle so the first line isn't lost
  Serial.println();
  Serial.printf("[boot] chip=%s rev=%d cores=%d flash=%uMB\n",
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
                ESP.getFlashChipSize() / (1024 * 1024));
}

// No delay() in loop: each task checks its own clock, so adding a sensor or a
// button later won't be blocked by the blink.
void loop() {
  unsigned long now = millis();

  if (now - lastBlink >= BLINK_MS) {
    lastBlink = now;
    ledOn = !ledOn;
    digitalWrite(LED_PIN, ledOn ? HIGH : LOW);
  }

  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    Serial.printf("[alive] uptime=%lus free_heap=%u\n", now / 1000, ESP.getFreeHeap());
  }
}
