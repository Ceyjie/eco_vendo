import os
import time

# Sysfs IDs from your Pinout:
# PA3 (Pin 15), PA2 (Pin 22), PC3 (Pin 24), PA21 (Pin 26)
RELAY_PINS = ["3", "2", "67", "21"]

def setup():
    print("--- Initializing Relay Pins (Active High) ---")
    for pin in RELAY_PINS:
        # Export pin if not already exported
        pin_path = f"/sys/class/gpio/gpio{pin}"
        if not os.path.exists(pin_path):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(pin)
            except Exception as e:
                print(f"Warning: Could not export pin {pin}: {e}")
        
        # Give the system a moment to create the file entries
        time.sleep(0.2)

        # Set to OUTPUT
        with open(f"{pin_path}/direction", "w") as f:
            f.write("out")

        # Set to LOW (0) initially (OFF for Active High relays)
        with open(f"{pin_path}/value", "w") as f:
            f.write("0")
        
        print(f"Relay ID {pin} Ready and OFF.")

def test_sequence():
    print("\n--- Starting Test Sequence ---")
    print("Each relay will click ON for 3 seconds, then OFF.")

    try:
        for pin in RELAY_PINS:
            pin_val_path = f"/sys/class/gpio/gpio{pin}/value"
            
            print(f"Testing Relay ID {pin} (ON)...")
            with open(pin_val_path, "w") as f:
                f.write("1")  # Logic 1 = Relay ON (Active High)

            time.sleep(3)

            print(f"Testing Relay ID {pin} (OFF)...")
            with open(pin_val_path, "w") as f:
                f.write("0")  # Logic 0 = Relay OFF (Active High)

            time.sleep(1)

        print("\nTest Complete!")
    except KeyboardInterrupt:
        print("\nTest aborted by user.")

if __name__ == "__main__":
    setup()
    test_sequence()
