import time, os

DOUT_PIN = "68"  # PC4 - Physical Pin 16
SCK_PIN  = "71"  # PC7 - Physical Pin 18

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

print("=== HX711 DIAGNOSTIC ===")
print()

# Step 1: Setup
print("1. Setting up pins...")
gpio_setup(DOUT_PIN, "in")
gpio_setup(SCK_PIN,  "out", "0")
print(f"   DOUT (PA8, pin 31) = input")
print(f"   SCK  (PA9, pin 33) = output, set LOW")
print()

# Step 2: Read DOUT idle state
print("2. Reading DOUT idle state (SCK=LOW)...")
for i in range(5):
    val = gpio_read(DOUT_PIN)
    print(f"   DOUT = {val}  ({'READY (0=good)' if val == 0 else 'NOT READY (1=bad)'})")
    time.sleep(0.2)
print()

# Step 3: Try manual clock pulses and watch DOUT change
print("3. Sending 25 clock pulses manually, watching DOUT...")
gpio_write(SCK_PIN, 0)
time.sleep(0.01)

# wait ready
deadline = time.time() + 2.0
while gpio_read(DOUT_PIN) != 0:
    if time.time() > deadline:
        print("   TIMEOUT: DOUT never went LOW. HX711 not responding.")
        print("   Check: VCC, GND, wiring to PA8/PA9")
        exit()
    time.sleep(0.001)

print("   DOUT went LOW — HX711 is ready!")

raw = 0
bits = []
for i in range(24):
    gpio_write(SCK_PIN, 1)
    time.sleep(0.0001)
    bit = gpio_read(DOUT_PIN)
    bits.append(bit)
    raw = (raw << 1) | bit
    gpio_write(SCK_PIN, 0)
    time.sleep(0.0001)

# 25th pulse
gpio_write(SCK_PIN, 1); time.sleep(0.0001)
gpio_write(SCK_PIN, 0); time.sleep(0.0001)

if raw & 0x800000:
    raw_signed = raw - 0x1000000
else:
    raw_signed = raw

print(f"   Bits: {''.join(str(b) for b in bits)}")
print(f"   Raw unsigned : {raw}")
print(f"   Raw signed   : {raw_signed}")
print()

# Step 4: Read 10 times
print("4. Reading 10 samples...")
for i in range(10):
    deadline = time.time() + 1.0
    while gpio_read(DOUT_PIN) != 0:
        if time.time() > deadline:
            print(f"   Sample {i+1}: TIMEOUT")
            break
        time.sleep(0.001)

    r = 0
    for _ in range(24):
        gpio_write(SCK_PIN, 1); time.sleep(0.0001)
        r = (r << 1) | gpio_read(DOUT_PIN)
        gpio_write(SCK_PIN, 0); time.sleep(0.0001)
    gpio_write(SCK_PIN, 1); time.sleep(0.0001)
    gpio_write(SCK_PIN, 0); time.sleep(0.0001)
    if r & 0x800000: r -= 0x1000000
    print(f"   Sample {i+1:2d}: {r}")
    time.sleep(0.1)

print()
print("=== DONE ===")
print("If raw values change when you press on load cell -> wiring OK, needs calibration")
print("If raw values are always 0 or same -> load cell wiring issue (E+/E-/A+/A-)")
print("If TIMEOUT -> HX711 power or DOUT pin issue")

