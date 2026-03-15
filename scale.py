import time, os, sys, select

# ─── Pin Configuration ────────────────────────────────────────────────────────
PIN_DT  = "8"    # PA8 / Physical Pin 24  (DOUT)
PIN_SCK = "9"    # PA9 / Physical Pin 21  (SCK)

# ─── Calibration ─────────────────────────────────────────────────────────────
calibration_factor = 441.17

# ─── Tuning Parameters ───────────────────────────────────────────────────────
SAMPLES          = 5
DEADZONE_G       = 2.0
OBJECT_THRESHOLD = 5.0
AUTO_TARE_LIMIT  = 2.5
AUTO_TARE_COUNTS = 10
OBJECT_CONFIRM   = 3
WARMUP_READS     = 20
WARMUP_DELAY     = 0.1
LOOP_DELAY       = 0.15
HX711_TIMEOUT    = 1.0
MAX_RETRIES      = 5

# ─── Globals ─────────────────────────────────────────────────────────────────
tare_offset = 0.0
empty_count = 0
scale_ready = False

# ─── GPIO ────────────────────────────────────────────────────────────────────
def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"{path}/value", "w") as f: f.write(value)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return 1  # default HIGH = not ready

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# ─── HX711 is_ready ──────────────────────────────────────────────────────────
def is_ready():
    return gpio_read(PIN_DT) == 0

# ─── HX711 wait_ready_timeout ────────────────────────────────────────────────
def wait_ready_timeout(timeout=HX711_TIMEOUT):
    deadline = time.time() + timeout
    while not is_ready():
        if time.time() > deadline:
            return False
        time.sleep(0.001)
    return True

# ─── HX711 read raw (24-bit signed) ──────────────────────────────────────────
def read_raw():
    if not wait_ready_timeout():
        return None
    raw = 0
    for _ in range(24):
        gpio_write(PIN_SCK, 1); time.sleep(0.000001)
        raw = (raw << 1) | gpio_read(PIN_DT)
        gpio_write(PIN_SCK, 0); time.sleep(0.000001)
    # 25th pulse = Channel A gain 128
    gpio_write(PIN_SCK, 1); time.sleep(0.000001)
    gpio_write(PIN_SCK, 0); time.sleep(0.000001)
    if raw & 0x800000:
        raw -= 0x1000000
    return raw

# ─── read_average ─────────────────────────────────────────────────────────────
def read_average(samples=10):
    total, count = 0, 0
    for _ in range(samples):
        r = read_raw()
        if r is not None:
            total += r; count += 1
    return total / count if count else None

# ─── Median filter (5 samples) ───────────────────────────────────────────────
def get_units():
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            readings.append((r - tare_offset) / calibration_factor)
    if not readings: return None
    readings.sort()
    return readings[len(readings) // 2]

# ─── Tare ─────────────────────────────────────────────────────────────────────
def tare():
    global tare_offset, empty_count
    avg = read_average(10)
    if avg is not None:
        tare_offset = avg
        empty_count = 0
        print("Manual tare. Zeroed.")
    else:
        print("Tare failed — no valid readings.")

# ─── Init / Re-init scale ─────────────────────────────────────────────────────
def init_scale():
    global scale_ready, tare_offset, empty_count
    if not is_ready():
        print(f"HX711 not responding. Check pins {PIN_DT} (DOUT) and {PIN_SCK} (SCK).")
        return False

    print("Stabilizing", end="", flush=True)
    for _ in range(WARMUP_READS):
        read_raw()
        time.sleep(WARMUP_DELAY)
        print(".", end="", flush=True)
    print()

    avg = read_average(10)
    if avg is None:
        print("Tare failed during init.")
        return False

    tare_offset = avg
    empty_count = 0
    print("Zeroed. Ready to weigh!")
    return True

# ─── Setup ───────────────────────────────────────────────────────────────────
def setup():
    global scale_ready
    print("\n─── HX711 Scale ────────────────────────────────")
    print("Remove all weight from scale.")

    gpio_setup(PIN_DT,  "in")
    gpio_setup(PIN_SCK, "out", "0")

    attempts = 0
    while not init_scale():
        attempts += 1
        if attempts >= MAX_RETRIES:
            print(f"FATAL: HX711 not found after {MAX_RETRIES} attempts. Halting.")
            while True: time.sleep(1)
        print(f"Retrying in 2s... ({attempts}/{MAX_RETRIES})")
        time.sleep(2)

    scale_ready = True
    print("────────────────────────────────────────────────\n")

# ─── Loop ────────────────────────────────────────────────────────────────────
def loop():
    global empty_count, calibration_factor

    confirm_count    = 0
    confirmed_weight = 0.0

    while True:
        if not scale_ready:
            time.sleep(0.5)
            continue

        # ── Serial commands (non-blocking) ───────────────────────────────────
        if select.select([sys.stdin], [], [], 0)[0]:
            cmd = sys.stdin.read(1).lower()
            if cmd == 't':
                tare()
            elif cmd == 'r':
                raw = read_average(10)
                print(f"Raw: {raw:.0f}" if raw else "Raw: failed")
            elif cmd == 'c':
                print("Enter new calibration factor:")
                try:
                    calibration_factor = float(input())
                    print(f"Calibration factor: {calibration_factor:.4f}")
                except: print("Invalid value.")
            elif cmd == '?':
                print("t=tare  r=raw  c=calibrate  ?=help  q=quit")
            elif cmd == 'q':
                print("Quit.")
                break

        # ── Check connection ─────────────────────────────────────────────────
        if not wait_ready_timeout():
            print("Check Wires: HX711 Connection Lost")
            time.sleep(1)
            if init_scale():
                print("Recovered.")
            else:
                print("Recovery failed. Check wiring.")
            continue

        # ── Get weight ───────────────────────────────────────────────────────
        weight = get_units()
        if weight is None:
            time.sleep(LOOP_DELAY)
            continue

        # ── Auto-tare when empty and drifted ─────────────────────────────────
        if abs(weight) < OBJECT_THRESHOLD:
            empty_count += 1
            if empty_count >= AUTO_TARE_COUNTS and abs(weight) > AUTO_TARE_LIMIT:
                tare()
                weight = 0.0
                print("[Auto-tare: drift corrected]")
        else:
            empty_count = 0

        # ── Deadzone ─────────────────────────────────────────────────────────
        if abs(weight) < DEADZONE_G:
            weight = 0.0

        # ── Hysteresis: confirm object is really there ────────────────────────
        if weight >= OBJECT_THRESHOLD:
            confirm_count += 1
            if confirm_count >= OBJECT_CONFIRM:
                confirmed_weight = weight
        else:
            confirm_count    = 0
            confirmed_weight = weight

        print(f"Weight: {confirmed_weight:.1f} g")
        time.sleep(LOOP_DELAY)

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup()
    loop()
