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

# Setup pins
gpio_setup(DOUT_PIN, "in")
gpio_setup(SCK_PIN,  "out", "0")

# Open file handles ONCE and keep them open
f_dout = open(f"/sys/class/gpio/gpio{DOUT_PIN}/value", "r")
f_sck  = open(f"/sys/class/gpio/gpio{SCK_PIN}/value",  "w")

def dout_read():
    f_dout.seek(0)
    return int(f_dout.read().strip())

def sck_write(val):
    f_sck.seek(0)
    f_sck.write(str(val))
    f_sck.flush()

def read_raw():
    # Wait for DOUT LOW
    deadline = time.time() + 1.0
    while dout_read() != 0:
        if time.time() > deadline:
            return None
        time.sleep(0.0001)

    raw = 0
    for _ in range(24):
        sck_write(1)
        bit = dout_read()
        sck_write(0)
        raw = (raw << 1) | bit

    # 25th pulse
    sck_write(1)
    sck_write(0)

    if raw & 0x800000:
        raw -= 0x1000000
    return raw

print(f"HX711 Raw Data Test")
print(f"  DOUT = PC4 (GPIO {DOUT_PIN}, Pin 16)")
print(f"  SCK  = PC7 (GPIO {SCK_PIN}, Pin 18)")
print(f"  DOUT idle = {dout_read()}")
print()
print("Reading raw values... press on load cell (Ctrl+C to stop)")
print()

try:
    while True:
        val = read_raw()
        if val is None:
            print("TIMEOUT")
        else:
            print(f"Raw: {val}")
        time.sleep(0.1)
except KeyboardInterrupt:
    sck_write(0)
    f_dout.close()
    f_sck.close()
    print("\nStopped.")
