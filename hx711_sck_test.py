import time, os

SCK_PIN  = "9"   # PA9
DOUT_PIN = "8"   # PA8

def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(value)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return -1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

print("=== SCK PIN TEST ===")
gpio_setup(SCK_PIN,  "out", "0")
gpio_setup(DOUT_PIN, "in")

# Test 1: Can we write and read back SCK?
print("\n1. SCK write/readback test (loop SCK to another pin to verify):")
for val in [0, 1, 0, 1, 0]:
    gpio_write(SCK_PIN, val)
    time.sleep(0.01)
    readback = gpio_read(SCK_PIN)
    print(f"   Write {val} -> Read {readback}  {'OK' if readback == val else 'FAIL'}")

# Test 2: Check direction
print("\n2. SCK direction:")
try:
    with open(f"/sys/class/gpio/gpio{SCK_PIN}/direction", "r") as f:
        print(f"   direction = {f.read().strip()}")
except: print("   Cannot read direction")

# Test 3: Toggle SCK slowly and watch DOUT
print("\n3. Slow clock test (100ms per pulse) — watch if DOUT changes:")
gpio_write(SCK_PIN, 0)
time.sleep(0.5)

# wait for DOUT ready
print(f"   DOUT before clocking: {gpio_read(DOUT_PIN)}")

for i in range(26):
    gpio_write(SCK_PIN, 1)
    time.sleep(0.1)
    dout = gpio_read(DOUT_PIN)
    gpio_write(SCK_PIN, 0)
    time.sleep(0.1)
    print(f"   Pulse {i+1:2d}: DOUT={dout}")

print(f"\n   DOUT after 26 pulses: {gpio_read(DOUT_PIN)}")
print("   If DOUT changed during pulses -> SCK is working")
print("   If DOUT stayed 1 the whole time -> SCK not reaching HX711")
