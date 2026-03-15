import time, os, sys, select

# ─── Pin Configuration ────────────────────────────────────────────────────────
# Orange Pi One Physical Pin 16 -> PC4 (68)
# Orange Pi One Physical Pin 18 -> PC7 (71)
DOUT_PIN = "68"  
SCK_PIN  = "71"  

# ─── Calibration ─────────────────────────────────────────────────────────────
# 1. Run script, press 't' to tare.
# 2. Place 100g weight. 
# 3. Press 'r' to get Raw Value.
# 4. New Factor = (Raw Value - Tare Offset) / 100
calibration_factor = 441.17 

# ─── Tuning Parameters ───────────────────────────────────────────────────────
SAMPLES          = 5
DEADZONE_G       = 1.5
OBJECT_THRESHOLD = 5.0
AUTO_TARE_LIMIT  = 2.0
AUTO_TARE_COUNTS = 15
WARMUP_READS     = 20
LOOP_DELAY       = 0.1
HX711_TIMEOUT    = 1.0

# ─── Globals ─────────────────────────────────────────────────────────────────
tare_offset = 0.0
empty_count = 0
fd_dout = None
fd_sck  = None

# ─── GPIO Low Level ──────────────────────────────────────────────────────────
def gpio_export(pin):
    if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.2)

def gpio_set_direction(pin, direction, value="0"):
    with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(value)

def setup_gpio():
    global fd_dout, fd_sck
    gpio_export(DOUT_PIN)
    gpio_export(SCK_PIN)
    gpio_set_direction(DOUT_PIN, "in")
    gpio_set_direction(SCK_PIN,  "out", "0")
    fd_dout = os.open(f"/sys/class/gpio/gpio{DOUT_PIN}/value", os.O_RDONLY)
    fd_sck  = os.open(f"/sys/class/gpio/gpio{SCK_PIN}/value",  os.O_WRONLY)

def dout():
    os.lseek(fd_dout, 0, 0)
    return os.read(fd_dout, 1) == b'0' # True if Data is LOW (Ready)

def sck(val):
    os.lseek(fd_sck, 0, 0)
    os.write(fd_sck, b'1' if val else b'0')

# ─── HX711 Logic ─────────────────────────────────────────────────────────────
def wait_ready(timeout=HX711_TIMEOUT):
    deadline = time.time() + timeout
    while not dout():
        if time.time() > deadline: return False
        time.sleep(0.001)
    return True

def read_raw():
    if not wait_ready(): return None
    
    raw = 0
    for _ in range(24):
        sck(1)
        # Micro-delay for Allwinner H3 pin stability
        # Remove if readings are too slow, add if readings are "noisy"
        # time.sleep(0.000001) 
        bit = 0 if dout() else 1
        sck(0)
        raw = (raw << 1) | bit
    
    # 25th pulse (Gain 128)
    sck(1)
    sck(0)

    # 2's Complement
    if raw & 0x800000:
        raw -= 0x1000000
    return raw

def read_average(samples=10):
    total, count = 0, 0
    for _ in range(samples):
        r = read_raw()
        if r is not None:
            total += r
            count += 1
    return total / count if count else None

def get_units():
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            # Removed the negative sign from previous version
            readings.append((r - tare_offset) / calibration_factor)
    if not readings: return None
    readings.sort()
    return readings[len(readings) // 2] # Median filter

def zero(samples=15):
    global tare_offset, empty_count
    print("Taring...", end="", flush=True)
    avg = read_average(samples)
    if avg is not None:
        tare_offset = avg
        empty_count = 0
        print(" Done.")
    else:
        print(" Failed.")

# ─── Main Interface ──────────────────────────────────────────────────────────
def setup():
    print("\n" + "="*40)
    print("      ORANGE PI ONE HX711 SCALE")
    print("="*40)
    setup_gpio()
    
    # Warmup
    for _ in range(WARMUP_READS):
        read_raw()
        time.sleep(0.05)
        
    zero()
    print("t=tare  r=raw  c=calibrate  q=quit")
    print("-"*40)

def loop():
    global empty_count, calibration_factor, tare_offset

    while True:
        # User Inputs
        if select.select([sys.stdin], [], [], 0)[0]:
            cmd = sys.stdin.read(1).lower()
            if cmd == 't': 
                zero()
            elif cmd == 'r':
                print(f"\n[DEBUG] Raw: {read_raw()} | Offset: {tare_offset:.0f}")
            elif cmd == 'q':
                sck(0)
                os.close(fd_dout)
                os.close(fd_sck)
                print("\nExiting...")
                break

        # Get Weight
        weight = get_units()
        if weight is None:
            print("Sensor timeout!")
            time.sleep(1)
            continue

        # Auto-Zero Logic
        if abs(weight) < OBJECT_THRESHOLD:
            empty_count += 1
            if empty_count >= AUTO_TARE_COUNTS and abs(weight) > AUTO_TARE_LIMIT:
                zero(5)
            weight = 0.0 if abs(weight) < DEADZONE_G else weight
        else:
            empty_count = 0

        sys.stdout.write(f"\rWeight: {weight:>8.1f} g    ")
        sys.stdout.flush()
        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    try:
        setup()
        loop()
    except KeyboardInterrupt:
        sck(0)
        print("\nStopped.")
