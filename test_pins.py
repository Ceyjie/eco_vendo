import OPi.GPIO as GPIO
import time

# Use physical pin numbering
GPIO.setmode(GPIO.BOARD)

# Using your physical pins: 8 (Start), 10 (Select), 12 (Confirm)
pins = [8, 10, 12]

for p in pins:
    # Set as input with Internal PULL-DOWN 
    # This keeps the pin at 0V until you press the button
    GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

print("--- GPIO TEST MODE (ACTIVE HIGH) ---")
print("Connect Pin 8, 10, or 12 to 3.3V (Pin 1) to test.")
print("Press Ctrl+C to stop.")

try:
    while True:
        for p in pins:
            # Check if pin receives 3.3V (High)
            if GPIO.input(p) == 1:
                print(f"HIGH SIGNAL ON PIN {p} DETECTED!")
                time.sleep(0.3) # Simple debounce
        time.sleep(0.05)
except KeyboardInterrupt:
    print("\nCleaning up...")
    GPIO.cleanup()
