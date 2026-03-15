import time, os, sys, select

# ─── Pin Configuration ────────────────────────────────────────────────────────
DOUT_PIN = "68"  # PC4 - Physical Pin 16
SCK_PIN  = "71"  # PC7 - Physical Pin 18

# ─── THE NEW CALIBRATION ─────────────────────────────────────────────────────
# Using your test data: (802918 - 467295) / 170g = 1974.25
calibration_factor = 1974.25 

# ─── Tuning ──────────────────────────────────────────────────────────────────
SAMPLES          = 10   # Increased for better stability
DEADZONE_G       = 2.0
LOOP_DELAY       = 0.2

# ─── Globals ─────────────────────────────────────────────────────────────────
tare_offset = 0.0
fd_dout = None
fd_sck  = None

def setup_gpio():
    global fd_dout, fd_sck
    for pin in [DOUT_PIN, SCK_PIN]:
        if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
    time.sleep(0.2)
    with open(f"/sys/class/gpio/gpio{DOUT_PIN}/direction", "w") as f: f.write("in")
    with open(f"/sys/class/gpio/gpio{SCK_PIN}/direction", "w") as f: f.write("out")
    fd_dout = os.open(f"/sys/class/gpio/gpio{DOUT_PIN}/value", os.O_RDONLY)
    fd_sck  = os.open(f"/sys/class/gpio/gpio{SCK_PIN}/value",  os.O_WRONLY)

def dout():
    os.lseek(fd_dout, 0, 0)
    return os.read(fd_dout, 1) == b'0'

def sck(val):
    os.lseek(fd_sck, 0, 0)
    os.write(fd_sck, b'1' if val else b'0')

def read_raw():
    deadline = time.time() + 1.0
    while not dout():
        if time.time() > deadline: return None
        time.sleep(0.001)
    
    raw = 0
    for _ in range(24):
        sck(1)
        bit = 0 if dout() else 1
        sck(0)
        raw = (raw << 1) | bit
    
    sck(1); sck(0) # 25th pulse
    if raw & 0x800000: raw -= 0x1000000
    return raw

def get_units():
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            # FLIPPED LOGIC: (Tare - Raw) because your sensor counts DOWN
            readings.append((tare_offset - r) / calibration_factor)
    if not readings: return None
    readings.sort()
    return readings[len(readings) // 2]

def zero():
    global tare_offset
    total, count = 0, 0
    for _ in range(20):
        r = read_raw()
        if r is not None:
            total += r; count += 1
    if count > 0: tare_offset = total / count

if __name__ == "__main__":
    setup_gpio()
    print("Zeroing scale... please wait.")
    zero()
    print(f"Ready. Offset: {tare_offset:.0f}")
    try:
        while True:
            w = get_units()
            if w is not None:
                if abs(w) < DEADZONE_G: w = 0.0
                print(f"\rWeight: {w:8.1f} g", end="", flush=True)
            time.sleep(LOOP_DELAY)
    except KeyboardInterrupt:
        print("\nExit.")
