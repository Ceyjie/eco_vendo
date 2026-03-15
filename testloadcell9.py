import time, os, sys, select

# ─── Pin Configuration ────────────────────────────────────────────────────────
DOUT_PIN = "68"  # PC4 - Physical Pin 16
SCK_PIN  = "71"  # PC7 - Physical Pin 18

# ─── Calibration ─────────────────────────────────────────────────────────────
# Based on your item: (Abs(Raw_with_170g - Tare_Offset)) / 170
calibration_factor = 1974.25 

# ─── Tuning Parameters ───────────────────────────────────────────────────────
SAMPLES          = 10   # Increased for stability
DEADZONE_G       = 2.0
OBJECT_THRESHOLD = 5.0
WARMUP_READS     = 30
LOOP_DELAY       = 0.2

# ─── Globals ─────────────────────────────────────────────────────────────────
tare_offset = 0.0
fd_dout = None
fd_sck  = None

# ─── GPIO Low Level ──────────────────────────────────────────────────────────
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

# ─── HX711 Read Logic (With H3 Timing Fixes) ─────────────────────────────────
def read_raw():
    # Wait for DOUT to go LOW (Ready)
    deadline = time.time() + 1.0
    while not dout():
        if time.time() > deadline: return None
        time.sleep(0.001)

    raw = 0
    for _ in range(24):
        sck(1)
        time.sleep(0.000002) # Essential delay for Orange Pi One
        bit = 0 if dout() else 1
        sck(0)
        time.sleep(0.000002) # Essential delay for Orange Pi One
        raw = (raw << 1) | bit

    # 25th pulse to set gain (128)
    sck(1)
    time.sleep(0.000002)
    sck(0)

    # Convert 24-bit signed to Python int
    if raw & 0x800000:
        raw -= 0x1000000
    return raw

def get_units():
    readings = []
    for _ in range(SAMPLES):
        r = read_raw()
        if r is not None:
            # Check polarity: if weight goes negative when pressing, swap (r - tare) to (tare - r)
            readings.append((r - tare_offset) / calibration_factor)
    if not readings: return None
    readings.sort()
    return readings[len(readings) // 2] # Median filter handles spikes

def zero():
    global tare_offset
    total, count = 0, 0
    for _ in range(30):
        r = read_raw()
        if r is not None:
            total += r; count += 1
    if count > 0: tare_offset = total / count

# ─── Main Interface ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    setup_gpio()
    print("\n" + "="*40)
    print("      ORANGE PI ONE HX711 SCALE")
    print("="*40)
    
    # Warmup
    for i in range(WARMUP_READS):
        read_raw()
        if i % 5 == 0: print(".", end="", flush=True)
    
    zero()
    print(f"\nReady. Offset: {int(tare_offset)}")
    print("Commands: t=tare, r=raw, q=quit")
    print("-"*40)

    try:
        while True:
            # Handle Inputs
            if select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.read(1).lower()
                if cmd == 't':
                    zero()
                    print("\nTared.")
                elif cmd == 'r':
                    r = read_raw()
                    print(f"\n[DEBUG] Raw: {r} | Diff: {int(r - tare_offset)}")
                elif cmd == 'q':
                    break

            # Process Weight
            weight = get_units()
            if weight is not None:
                if abs(weight) < DEADZONE_G: weight = 0.0
                sys.stdout.write(f"\rWeight: {weight:>8.1f} g    ")
                sys.stdout.flush()
            
            time.sleep(LOOP_DELAY)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd_dout)
        os.close(fd_sck)
        print("\nClean exit.")
