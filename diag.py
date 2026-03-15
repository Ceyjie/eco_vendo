import gpiod
import time

PIN_DT = 68  # Physical Pin 16 / PC4
CHIP_PATH = "/dev/gpiochip0"

print(f"Monitoring DT pin {PIN_DT} on {CHIP_PATH}...")

try:
    # Request the line as an input
    with gpiod.request_lines(
        CHIP_PATH,
        consumer="hx711-diag",
        config={PIN_DT: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)}
    ) as request:
        
        print("Checking signal... (Press Ctrl+C to stop)")
        print("1 = Idle/High | 0 = Data Ready/Low")
        print("-" * 30)

        while True:
            # Read the value
            val = request.get_value(PIN_DT)
            # Use .value to get the integer 0 or 1
            print(f"Current DT Value: {val.value}", end="\r")
            time.sleep(0.1)

except KeyboardInterrupt:
    print("\nStopped.")
except Exception as e:
    print(f"\nError: {e}")
