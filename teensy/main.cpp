
#include "WProgram.h"

#include <stdint.h>

#define PIN_DOOR_SENSOR 20
#define PIN_LDR 22


extern "C" int main(void) {
    uint32_t ldr = 0;
    int door_status, new_status = 0;
    elapsedMillis timeElapsedLDR;
    elapsedMillis timeElapsedDoor;

    pinMode(PIN_DOOR_SENSOR, INPUT);
    Serial.begin(9600);
    door_status = digitalRead(PIN_DOOR_SENSOR);

    for (;;) {
        ldr = analogRead(PIN_LDR);
        if (ldr > 500 && timeElapsedLDR > 1000) {
            timeElapsedLDR = 0;
            Serial.write(20);
        }

        new_status = digitalRead(PIN_DOOR_SENSOR);
        if (new_status != door_status && timeElapsedDoor > 200) {
            if (new_status == 0) {
                Serial.write(10);
            } else {
                Serial.write(11);
            }
            door_status = new_status;
            timeElapsedDoor = 0;
        }
    }
}
