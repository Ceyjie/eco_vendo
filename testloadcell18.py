import time, os, sys, select

# --- Pin Configuration (Orange Pi One PC4/PC7) ---
DOUT_PIN = "68"
SCK_PIN  = "71"

# --- Calibration ---
calibration_factor = 441.17

# --- FAST TUNING PARAMETERS ---
SAMPLES          = 3     # Reduced from 5 for faster median
DEADZONE_G       = 2.0
OBJECT_THRESHOLD = 5.0
AUTO_TARE_LIMIT  = 2.5
AUTO_TARE_COUNTS = 5     # Faster drift correction
LOOP_DELAY       = 0.1   # Faster UI response
HX711_TIMEOUT    = 0.5   # Shorter timeout

# --- Globals ---
tare_offset  = 0.0
empty_count  = 0
fd_dout      = None
fd_sck       = None

# --- GPIO Engine ---
def gpio_export(pin):
    if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass

def gpio_set_direction(pin, direction, value="0"):
    with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(value)

def gpio_begin():
    global fd_dout, fd_sck
    gpio_export(DOUT_PIN)
    gpio_export(SCK_PIN)
    gpio_set_direction(DOUT_PIN, "in")
    gpio_set_direction(SCK_PIN,  "out", "0")
    fd_dout = os.open(f"/sys/class/gpio/gpio{DOUT_PIN}/value", os.O_RDONLY)
    fd_sck  = os.open(f"/sys/class/gpio/gpio{SCK_PIN}/value",  os.O_WRONLY)

def dout():
    os.lseek(fd_dout, 0, 0)
    return os.read(fd_dout, 1) == b'0'

def sck(val):
    os.lseek(fd_sck, 0, 0)
    os.write(fd_sck, b'1' if val else b'0')

def read_raw():
    # Fast check for data ready
    deadline = time.time() + HX711_TIMEOUT
    while not dout():
        if time.time() > deadline: return None
    
    raw = 0
    for _ in range(24):
        sck(1)
        bit = 0 if dout() else 1
        sck(0)
        raw = (raw << 1) | bit
    sck(1); sck(0) # Channel A Gain 128
    if raw & 0x800000: raw -= 0x1000000
    return raw

# --- Optimized Fast Init ---
def fast_init():
    global tare_offset
    print("Fast-Booting Scale...", end="", flush=True)
    
    # 1. Very short warmup (just clear the HX711 buffer)
    for _ in range(3):
        read_raw()
    
    # 2. Fast Tare (take 5 quick samples, use median to ignore spikes)
    samples = []
    for _ in range(7):
        r = read_raw()
        if r is not None: samples.append(r)
        time.sleep(0.01)
    
    if len(samples) < 3:
        print("\nERROR: Scale not responding.")
        return False
    
    samples.sort()
    tare_offset = samples[len(samples)//2] # Use median for instant stability
    print(" [READY]")
    return True

def get_units():
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            readings.append((r - tare_offset) / calibration_factor)
    if not readings: return None
    readings.sort()
    return readings[len(readings) // 2]

def setup():
    gpio_begin()
    if not fast_init():
        sys.exit(1)

def loop():
    global empty_count
    while True:
        weight = get_units()
        if weight is None: continue

        # Fast Drift Correction
        if abs(weight) < OBJECT_THRESHOLD:
            empty_count += 1
            if empty_count >= AUTO_TARE_COUNTS:
                # Instant background tare
                r = read_raw()
                if r: tare_offset = r
                empty_count = 0
                weight = 0.0
        else:
            empty_count = 0

        # Apply Deadzone
        if abs(weight) < DEADZONE_G: weight = 0.0
        
        sys.stdout.write(f"\rWeight: {weight:7.1f} g  ")
        sys.stdout.flush()
        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    try:
        setup()
        loop()
    except KeyboardInterrupt:
        sck(0)
        print("\nStopped.")
