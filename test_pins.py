import OPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BOARD)
# Test Pin 8 (Start), 10 (Select), 12 (Confirm)
pins = [8, 10, 12]

for p in pins:
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

print("--- GPIO TEST MODE ---")
print("Connect Pin 8, 10, or 12 to GROUND (Pin 6) to test.")

try:
    while True:
        for p in pins:
            if GPIO.input(p) == 0:
                print(f"BUTTON ON PIN {p} DETECTED!")
        time.sleep(0.1)
except KeyboardInterrupt:
    GPIO.cleanup()
