import os
import time

# Sysfs IDs for Orange Pi One
PINS = {
    "START": "13",    # Physical Pin 16
    "CONFIRM": "110", # Physical Pin 27
    "SELECT": "14"     # Physical Pin 18
}

def setup():
    print("--- Initializing Pins ---")
    for name, pin in PINS.items():
        # Export pin if not already exported
        if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
            with open("/sys/class/gpio/export", "w") as f:
                f.write(pin)
            time.sleep(0.2)
        
        # Set direction to input
        with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f:
            f.write("in")
        print(f"{name} (GPIO {pin}) ready.")

def monitor():
    print("\n--- Monitoring (Active Low) ---")
    print("Logic: 1 = Open, 0 = PRESSED/GND")
    print("Press Ctrl+C to exit\n")
    
    try:
        while True:
            results = []
            for name, pin in PINS.items():
                with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                    val = f.read().strip()
                    status = "[PRESS]" if val == "0" else "      "
                    results.append(f"{name}: {val} {status}")
            
            # Print on one line using carriage return
            print(" | ".join(results), end="\r")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nExiting...")

if __name__ == "__main__":
    setup()
    monitor()
