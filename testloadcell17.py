import time, os, sys, select

# ─── Pin Configuration ────────────────────────────────────────────────────────
DOUT_PIN = "68"  # PC4 - Physical Pin 16
SCK_PIN  = "71"  # PC7 - Physical Pin 18

# ─── Calibration ─────────────────────────────────────────────────────────────
calibration_factor = 441.17  # original value

# ─── Tuning Parameters ───────────────────────────────────────────────────────
SAMPLES         = 5
DEADZONE_G      = 2.0
OBJECT_THRESHOLD= 5.0
AUTO_TARE_LIMIT = 2.5
AUTO_TARE_COUNTS= 30   # increased — prevent false auto-tare
OBJECT_CONFIRM  = 3
WARMUP_READS    = 20    # back to 20 for better stability
WARMUP_DELAY    = 0.1   # back to 0.1s
LOOP_DELAY      = 0.15
HX711_TIMEOUT   = 1.0
MAX_RETRIES     = 5

# ─── Globals ─────────────────────────────────────────────────────────────────
tare_offset  = 0.0
empty_count  = 0
scale_ready  = False
fd_dout      = None
fd_sck       = None

# ─── GPIO ────────────────────────────────────────────────────────────────────
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

# ─── HX711 ───────────────────────────────────────────────────────────────────
def is_ready():
    return dout()

def wait_ready_timeout(timeout=HX711_TIMEOUT):
    deadline = time.time() + timeout
    while not is_ready():
        if time.time() > deadline: return False
        time.sleep(0.001)
    return True

def read_raw():
    if not wait_ready_timeout(): return None
    raw = 0
    for _ in range(24):
        sck(1)
        bit = 0 if dout() else 1
        sck(0)
        raw = (raw << 1) | bit
    sck(1); sck(0)
    if raw & 0x800000: raw -= 0x1000000
    return raw

def read_average(samples=10):
    total, count = 0, 0
    for _ in range(samples):
        r = read_raw()
        if r is not None: total += r; count += 1
    return total / count if count else None

def get_units():
    global calibration_factor
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            val = (r - tare_offset) / calibration_factor
            readings.append(val)
    if not readings: return None
    readings.sort()
    result = readings[len(readings) // 2]
    # Auto-flip: if negative, negate calibration factor permanently
    if result < -DEADZONE_G:
        calibration_factor = -abs(calibration_factor)
        result = -result
    return result

def tare(samples=10):
    global tare_offset, empty_count
    avg = read_average(samples)
    if avg is not None:
        tare_offset = avg
        empty_count = 0

def set_scale(factor):
    global calibration_factor
    calibration_factor = factor

# ─── Initialize / Re-initialize ──────────────────────────────────────────────
def init_scale():
    global scale_ready, empty_count, tare_offset

    if not is_ready():
        print(f"HX711 not responding. Check pins {DOUT_PIN} (DOUT) and {SCK_PIN} (SCK).")
        return False

    set_scale(calibration_factor)

    # ── Warmup (faster) ──────────────────────────────────────────────────────
    print("Stabilizing", end="", flush=True)
    for _ in range(WARMUP_READS):
        read_raw()
        time.sleep(WARMUP_DELAY)
        print(".", end="", flush=True)
    print()

    # ── Auto-zero: take 50 samples and average ───────────────────────────────
    print("Auto-zeroing", end="", flush=True)

    while True:
        total, count = 0, 0
        for _ in range(50):
            r = read_raw()
            if r is not None:
                total += r
                count += 1
            print(".", end="", flush=True)
        print()

        if count == 0:
            print("WARN: No valid samples. Retrying...")
            continue

        tare_offset = total / count
        empty_count = 0
        print(f"Zeroed. Offset={tare_offset:.0f}  Samples={count}")

        # ── Verify: read 10 samples and check if near zero ───────────────────
        print("Verifying...", end="", flush=True)
        verify = []
        for _ in range(10):
            r = read_raw()
            if r is not None:
                verify.append((r - tare_offset) / calibration_factor)

        if not verify:
            print(" No readings. Retrying...")
            continue

        verify.sort()
        check = abs(verify[len(verify) // 2])
        print(f" Check={check:.1f}g")

        if check <= 5.0:
            # Zero is accurate
            print("Zero OK.")
            return True
        else:
            # Abnormal — re-tare
            print(f"Abnormal reading ({check:.1f}g). Re-zeroing...")
            print("Auto-zeroing", end="", flush=True)

# ─── Setup ───────────────────────────────────────────────────────────────────
def setup():
    global scale_ready
    print("\n─── HX711 Scale ───────────────────────────────")
    print("Remove all weight from scale.")
    gpio_begin()

    attempts = 0
    while not init_scale():
        attempts += 1
        if attempts >= MAX_RETRIES:
            print(f"FATAL: HX711 not found after {MAX_RETRIES} attempts. Halting.")
            while True: time.sleep(1)
        print(f"Retrying in 2s... ({attempts}/{MAX_RETRIES})")
        time.sleep(2)

    scale_ready = True
    print("───────────────────────────────────────────────\n")

# ─── Loop ────────────────────────────────────────────────────────────────────
def loop():
    global empty_count, scale_ready

    confirm_count    = 0
    confirmed_weight = 0.0

    while True:
        if not scale_ready:
            time.sleep(0.5)
            continue

        serial_event()

        if not wait_ready_timeout():
            print("Check Wires: HX711 Connection Lost")
            time.sleep(1)
            if init_scale(): print("Recovered.")
            else: print("Recovery failed. Check wiring.")
            continue

        weight = get_units()
        if weight is None:
            time.sleep(LOOP_DELAY)
            continue

        if abs(weight) < OBJECT_THRESHOLD:
            empty_count += 1
            if empty_count >= AUTO_TARE_COUNTS and abs(weight) > AUTO_TARE_LIMIT:
                tare()
                empty_count = 0
                weight = 0.0
                print("[Auto-tare: drift corrected]")
        else:
            empty_count = 0

        if abs(weight) < DEADZONE_G: weight = 0.0

        if weight >= OBJECT_THRESHOLD:
            confirm_count += 1
            if confirm_count >= OBJECT_CONFIRM:
                confirmed_weight = weight
        else:
            confirm_count    = 0
            confirmed_weight = weight

        print(f"Weight: {confirmed_weight:.1f} g")
        time.sleep(LOOP_DELAY)

# ─── Serial Event ────────────────────────────────────────────────────────────
def serial_event():
    global calibration_factor
    if not select.select([sys.stdin], [], [], 0)[0]:
        return
    cmd = sys.stdin.read(1).lower()
    if cmd == 't':
        tare()
        empty_count_reset()
        print("Manual tare. Zeroed.")
    elif cmd == 'r':
        print(f"Raw: {read_average(10):.0f}")
    elif cmd == 'c':
        print("Enter new calibration factor:")
        try:
            val = float(input())
            set_scale(val)
            print(f"Calibration factor: {calibration_factor:.4f}")
        except: pass
    elif cmd == '?':
        print("t=tare  r=raw  c=calibrate  ?=help  q=quit")
    elif cmd == 'q':
        sck(0)
        os.close(fd_dout)
        os.close(fd_sck)
        print("Exiting.")
        sys.exit(0)

def empty_count_reset():
    global empty_count
    empty_count = 0

# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        setup()
        loop()
    except KeyboardInterrupt:
        sck(0)
        print("\nStopped.")
