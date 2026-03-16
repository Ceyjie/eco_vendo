import time, os, sys, json

# --- Pin Configuration ---
DOUT_PIN = "68"   # PC4 - Physical Pin 16
SCK_PIN  = "71"   # PC7 - Physical Pin 18

# --- Calibration ---
calibration_factor = 441.17

# --- Tuning ---
SAMPLES          = 3
DEADZONE_G       = 2.0
OBJECT_THRESHOLD = 5.0
AUTO_TARE_LIMIT  = 2.5
AUTO_TARE_COUNTS = 5
LOOP_DELAY       = 0.05   # ~20 reads/sec
HX711_TIMEOUT    = 0.5

# --- Shared IPC files ---
WEIGHT_FILE = "/tmp/eco_weight.json"
CMD_FILE    = "/tmp/eco_cmd.txt"

# --- Globals ---
tare_offset = 0.0
empty_count = 0
fd_dout     = None
fd_sck      = None

# ─── GPIO ────────────────────────────────────────────────────
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

# ─── HX711 ───────────────────────────────────────────────────
def read_raw():
    deadline = time.time() + HX711_TIMEOUT
    while not dout():
        if time.time() > deadline: return None
    raw = 0
    for _ in range(24):
        sck(1)
        bit = 0 if dout() else 1
        sck(0)
        raw = (raw << 1) | bit
    sck(1); sck(0)
    if raw & 0x800000: raw -= 0x1000000
    return raw

# ─── Auto-calibrate on startup ───────────────────────────────
def fast_init():
    global tare_offset
    print("Auto-calibrating...", end="", flush=True)

    # Clear HX711 buffer
    for _ in range(3):
        read_raw()

    # Take 7 samples, use median to ignore spikes
    samples = []
    for _ in range(7):
        r = read_raw()
        if r is not None and r != -1:
            samples.append(r)
        time.sleep(0.01)

    if len(samples) < 3:
        print(" ERROR: Scale not responding.")
        return False

    samples.sort()
    tare_offset = samples[len(samples) // 2]
    print(f" [READY] Offset={tare_offset:.0f}")
    return True

# ─── Re-tare (called by TARE command from main21.py) ─────────
def do_tare():
    global tare_offset, empty_count
    print("Re-taring...", end="", flush=True)
    for _ in range(3):
        read_raw()
    samples = []
    for _ in range(7):
        r = read_raw()
        if r is not None and r != -1:
            samples.append(r)
        time.sleep(0.05)
    if len(samples) >= 3:
        samples.sort()
        tare_offset = samples[len(samples) // 2]
        empty_count = 0
        print(f" Done. Offset={tare_offset:.0f}")
    else:
        print(" Failed.")

# ─── Write weight to shared file ─────────────────────────────
def write_weight(grams):
    try:
        with open(WEIGHT_FILE, "w") as f:
            json.dump({
                "grams": round(grams, 1),
                "tare":  round(tare_offset, 0),
                "ts":    time.time()
            }, f)
    except: pass

# ─── Check for commands from main21.py ───────────────────────
def check_cmd():
    if os.path.exists(CMD_FILE):
        try:
            with open(CMD_FILE, "r") as f:
                cmd = f.read().strip()
            os.remove(CMD_FILE)
            if cmd == "TARE":
                do_tare()
        except: pass

# ─── Setup ───────────────────────────────────────────────────
def setup():
    gpio_begin()
    if not fast_init():
        sys.exit(1)
    write_weight(0.0)

# ─── Main loop ───────────────────────────────────────────────
def loop():
    global empty_count, tare_offset, calibration_factor

    while True:
        check_cmd()

        r = read_raw()
        if r is None or r == -1:
            time.sleep(LOOP_DELAY)
            continue

        grams = (r - tare_offset) / calibration_factor

        # Auto-flip if negative
        if grams < -2.0:
            calibration_factor = -abs(calibration_factor)
            grams = -grams

        # Auto drift correction
        if abs(grams) < OBJECT_THRESHOLD:
            empty_count += 1
            if empty_count >= AUTO_TARE_COUNTS:
                tare_offset = r
                empty_count = 0
                grams = 0.0
        else:
            empty_count = 0

        # Deadzone
        if abs(grams) < DEADZONE_G:
            grams = 0.0

        write_weight(grams)

        sys.stdout.write(f"\rWeight: {grams:7.1f} g  ")
        sys.stdout.flush()
        time.sleep(LOOP_DELAY)

if __name__ == "__main__":
    try:
        setup()
        loop()
    except KeyboardInterrupt:
        sck(0)
        print("\nStopped.")
