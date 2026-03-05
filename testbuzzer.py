import time
import os

PIN_BUZZER = "0"

def gpio_setup(pin, direction="out", value="0"):
    if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
        try:
            with open("/sys/class/gpio/export", "w") as f:
                f.write(pin)
        except:
            print(f"Error: Could not export GPIO {pin}")
            return
    time.sleep(0.1)
    with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f:
        f.write(direction)
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write(value)

def gpio_write(pin, val):
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write(str(val))

# --- RUN TEST ---
print(f"Starting Buzzer Test on GPIO {PIN_BUZZER}...")
gpio_setup(PIN_BUZZER)

try:
    for i in range(5):
        print(f"Beep {i+1}")
        gpio_write(PIN_BUZZER, 1) # ON
        time.sleep(0.2)
        gpio_write(PIN_BUZZER, 0) # OFF
        time.sleep(0.5)
    print("Test Complete.")
except KeyboardInterrupt:
    gpio_write(PIN_BUZZER, 0)
    print("\nStopped.")
