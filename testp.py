import gpiod
import time
import os
from gpiod.line import Direction, Bias

# --- CONFIG ---
PINS_IN = {
    "START (PA13)": 13,
    "SELECT (PA14)": 14,
    "CONFIRM (PD14)": 110,
    "IR_BOTTOM (PA6)": 6,
    "IR_TOP (PA1)": 1
}
CHIP_PATH = "/dev/gpiochip0"

def cleanup_sysfs():
    """Releases pins from the old sysfs interface to prevent 'Device Busy' errors"""
    print("Checking for busy pins...")
    for name, offset in PINS_IN.items():
        sysfs_path = f"/sys/class/gpio/gpio{offset}"
        if os.path.exists(sysfs_path):
            try:
                with open("/sys/class/gpio/unexport", "w") as f:
                    f.write(str(offset))
                print(f"  Released pin {offset}")
            except:
                pass
    time.sleep(0.2) # Give kernel time to update

def monitor():
    cleanup_sysfs()
    print(f"Connecting to {CHIP_PATH}...")
    
    offsets = list(PINS_IN.values())
    names = list(PINS_IN.keys())

    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="eco-test",
            config={
                tuple(offsets): gpiod.LineSettings(
                    direction=Direction.INPUT,
                    bias=Bias.PULL_UP
                )
            }
        ) as request:
            
            print("\nSUCCESS: Monitoring with Internal Pull-ups")
            print("1 = Idle | 0 = Triggered (GND)")
            print("-" * 50)

            while True:
                line_values = request.get_values()
                results = [f"{names[i]}: {line_values[i].value}" for i in range(len(names))]
                print(" | ".join(results), end="\r")
                time.sleep(0.1)

    except Exception as e:
        print(f"\nError: {e}")
        print("\nTIP: If still 'Busy', a reboot will force-clear all GPIO claims.")

if __name__ == "__main__":
    monitor()
