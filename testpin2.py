import os
import time

# --- CONFIG (Direct Kernel IDs for Allwinner H3) ---
PINS = {
    "START": "13",
    "SELECT": "14",
    "CONFIRM": "110",
    "IR_BOTTOM": "6",
    "IR_TOP": "1",
    "BUZZER": "0"
}

def reset_all_gpios():
    """Forcefully releases all pins from the kernel"""
    print("[1/3] Resetting all GPIO exports...")
    if os.path.exists("/sys/class/gpio/"):
        for folder in os.listdir("/sys/class/gpio/"):
            if folder.startswith("gpio"):
                pin_num = folder.replace("gpio", "")
                try:
                    with open("/sys/class/gpio/unexport", "w") as f:
                        f.write(pin_num)
                except:
                    pass
    print("      Done.")

def setup_pins():
    """Exports pins and sets them to input mode"""
    print("[2/3] Initializing Pins via Sysfs...")
    for name, pin in PINS.items():
        try:
            if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(pin)
            time.sleep(0.1)

            # Set Direction
            direction = "out" if name == "BUZZER" else "in"
            with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f:
                f.write(direction)
            print(f"      {name} [ID {pin}] is Ready.")
        except Exception as e:
            print(f"      Error setting up {name}: {e}")

def monitor():
    """Live monitor of pin states"""
    print("[3/3] MONITORING... (Press Ctrl+C to stop)")
    print("-" * 60)
    print("EXPECTED: 1 (Idle/High), 0 (Pressed/Grounded)")
    print("-" * 60)

    try:
        while True:
            results = []
            for name, pin in PINS.items():
                if name == "BUZZER": continue
                try:
                    with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
                        val = f.read().strip()
                        status = " [!] " if val == "0" else "     "
                        results.append(f"{name}: {val}{status}")
                except:
                    results.append(f"{name}: ERR")

            print(" | ".join(results), end="\r")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n\nCleaning up and exiting...")

if __name__ == "__main__":
    reset_all_gpios()
    setup_pins()
    monitor()
