import time, threading, os, subprocess, json, uuid, signal, mmap, struct, queue
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory
from RPLCD.i2c import CharLCD
import gpiod
from gpiod.line import Direction, Bias

# ═══════════════════════════════════════════════════════════
# PIN CONFIG
# ═══════════════════════════════════════════════════════════
PIN_IR_BOTTOM_OFF = 6    
PIN_IR_TOP_OFF    = 1    
CHIP_PATH         = "/dev/gpiochip0" 

PIN_IR_BOTTOM  = "6"
PIN_IR_TOP     = "1"
PIN_BUZZER     = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS    = ["3", "2", "67", "21"]
PIN_SERVO      = "10"
SLOT_NAMES     = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE        = "eco_database.json"
ADMIN_PASSWORD = "1234"

# WEIGHT RANGES
WEIGHT_MIN_VALID  = 8
WEIGHT_SMALL_MIN, WEIGHT_SMALL_MAX = 9, 16
WEIGHT_NATURE_MIN, WEIGHT_NATURE_MAX = 16, 19
WEIGHT_BIG_MIN, WEIGHT_BIG_MAX = 29, 39

# ═══════════════════════════════════════════════════════════
# SERVO CONTROL (DIRECT DRIVE)
# ═══════════════════════════════════════════════════════════
PULSE_MIN_MS, PULSE_MAX_MS, PERIOD_MS = 0.5, 2.5, 20.0
PA_BASE, PA_DAT_OFF, PA10_BIT = 0x01C20800, 0x10, (1 << 10)
_servo_mem = None
_servo_sysfs = False

def servo_init():
    global _servo_mem, _servo_sysfs
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        _servo_mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=PA_BASE & ~0xFFF)
        os.close(fd)
        print("Servo: Memory-Map initialized.")
    except Exception as e:
        _servo_sysfs = True
        print(f"Servo: Sysfs fallback ({e})")
        path = f"/sys/class/gpio/gpio{PIN_SERVO}"
        if not os.path.exists(path):
            try:
                with open("/sys/class/gpio/export", "w") as f: f.write(PIN_SERVO)
                time.sleep(0.1)
                with open(f"{path}/direction", "w") as f: f.write("out")
            except: pass

def servo_set_pin(val):
    if not _servo_sysfs and _servo_mem:
        off = (PA_BASE & 0xFFF) + PA_DAT_OFF
        cur = struct.unpack_from("<I", _servo_mem, off)[0]
        struct.pack_into("<I", _servo_mem, off, cur | PA10_BIT if val else cur & ~PA10_BIT)
    else:
        try:
            with open(f"/sys/class/gpio/gpio{PIN_SERVO}/value", "w") as f: f.write("1" if val else "0")
        except: pass

def servo_goto(deg, pulses=100):
    """Direct pulsing. 100 pulses = ~2 seconds of active motor power."""
    deg = max(0, min(180, deg))
    pulse_ms = PULSE_MIN_MS + (deg / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    p_s, t_s = pulse_ms / 1000.0, PERIOD_MS / 1000.0
    for _ in range(pulses):
        t0 = time.perf_counter()
        servo_set_pin(1)
        while (time.perf_counter() - t0) < p_s: pass
        servo_set_pin(0)
        while (time.perf_counter() - t0) < t_s: pass
    servo_set_pin(0) # Ensure pin is low

# ═══════════════════════════════════════════════════════════
# SYSTEM STATE & DB
# ═══════════════════════════════════════════════════════════
session_data = {"state": "IDLE", "count": 0, "active_user": None, "selected_slot": 0, "add_time_choice": 1, "last_activity": time.time(), "last_bottle_msg": ""}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}
admin_data = {"active": False, "user_index": 0, "hold_start": 0, "holding": False}
bottle_lock = threading.Lock()
lcd_lock = threading.Lock()
_ir_reset = False

def load_db():
    if not os.path.exists(DB_FILE): return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# ═══════════════════════════════════════════════════════════
# GPIO & LCD HELPERS
# ═══════════════════════════════════════════════════════════
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
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

def beep_now(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.08)
        gpio_write(PIN_BUZZER, 0); time.sleep(0.04)

lcd = None
current_lcd_lines = ["", "", "", ""]

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear(); return
        except: lcd = None

def lcd_write_force(new_lines):
    global current_lcd_lines, lcd
    if not lcd: init_lcd(); return
    try:
        safe_lines = [str(line).ljust(20)[:20] for line in new_lines]
        for i, line in enumerate(safe_lines):
            if line != current_lcd_lines[i]:
                lcd.cursor_pos = (i, 0); lcd.write_string(line); current_lcd_lines[i] = line
    except: lcd = None; init_lcd()

# ═══════════════════════════════════════════════════════════
# BOTTLE PROCESSING (THE FIX)
# ═══════════════════════════════════════════════════════════
def hx_get_grams():
    try:
        with open("/tmp/eco_weight.json", "r") as f:
            d = json.load(f)
            return d.get("grams") if time.time()-d.get("ts") < 1.0 else None
    except: return None

def classify_bottle(w):
    if w < WEIGHT_MIN_VALID: return 0, None, "   NOT A BOTTLE!    ", "   Bottles only!    "
    if WEIGHT_SMALL_MIN <= w <= WEIGHT_SMALL_MAX: return 1, "SMALL", "  SMALL BOTTLE      ", "      +1 POINT!      "
    if WEIGHT_NATURE_MIN <= w <= WEIGHT_NATURE_MAX: return 2, "NATURE", "  NATURE SPRING     ", "      +2 POINTS!     "
    if WEIGHT_BIG_MIN <= w <= WEIGHT_BIG_MAX: return 2, "BIG", "  BIG BOTTLE        ", "      +2 POINTS!     "
    return 0, None, "  INVALID BOTTLE!   ", "  Check weight...   "

def process_bottle():
    with bottle_lock:
        with lcd_lock:
            global _ir_reset
            lcd_write_force(["    BOTTLE SENSED   ", "    Checking...     ", "  Place on sensor   ", "  Hold still...     "])
            
            samples = []
            for _ in range(12):
                w = hx_get_grams()
                if w is not None and w > WEIGHT_MIN_VALID: samples.append(w)
                if len(samples) >= 5: break
                time.sleep(0.3)

            if len(samples) < 3:
                lcd_write_force(["    NO BOTTLE       ", "    DETECTED!       ", "  Retry carefully   ", ""])
                beep_now(3); time.sleep(2); _ir_reset = True; return

            weight = sorted(samples)[len(samples)//2]
            pts, label, line1, line2 = classify_bottle(weight)

            if pts > 0:
                lcd_write_force(["    ACCEPTED!       ", line1, line2, "    Processing...    "])
                
                # 1. DROP BOTTLE (Go to 0)
                servo_goto(0, pulses=100)
                
                # Update Data
                session_data["count"] += pts
                db = load_db()
                db["total_bottles"] += 1
                db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label])
                save_db(db); beep_now(pts)

                # 2. WAIT AND RETURN (THE FIX)
                # We wait 2 seconds for the bottle to clear, then force return.
                lcd_write_force(["  REMOVING BOTTLE   ", "  Please wait...    ", "", ""])
                time.sleep(2.0) 
                servo_goto(90, pulses=100) # Force move back to 90

                lcd_write_force([line1, line2, f"  Total: {session_data['count']} pts", "  Keep recycling!   "])
                time.sleep(2)
            else:
                servo_goto(90, pulses=80) # Ensure we are at standby
                lcd_write_force(["    INVALID ITEM!   ", line1, line2, "    Try again...     "])
                beep_now(3); time.sleep(2)
            
            _ir_reset = True

# ═══════════════════════════════════════════════════════════
# HARDWARE LOOPS
# ═══════════════════════════════════════════════════════════
def hardware_loop():
    global _ir_reset
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}; last_press = {p: 0.0 for p in btn_pins}
    ir_cooldown = 0
    
    # Initialize IR via gpiod if possible
    _ir_request = None
    try:
        _ir_request = gpiod.request_lines(CHIP_PATH, consumer="eco-ir",
            config={(PIN_IR_BOTTOM_OFF, PIN_IR_TOP_OFF): gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)})
    except: pass

    while True:
        now = time.time()
        for p in btn_pins:
            v = gpio_read(p)
            if v == 0 and last_val[p] == 1 and (now - last_press[p]) > 0.3:
                last_press[p] = now
                if p == PIN_BTN_START and session_data["state"] == "IDLE":
                    session_data.update({"state": "INSERTING", "count": 0, "last_activity": now}); beep_now(1)
                elif p == PIN_BTN_CONFIRM and session_data["state"] == "INSERTING":
                    session_data["state"] = "IDLE" if session_data["count"] == 0 else "SELECTING"
            last_val[p] = v

        # IR Trigger
        if session_data["state"] == "INSERTING" and now > ir_cooldown and not bottle_lock.locked():
            bot, top = (0, 0)
            if _ir_request:
                vals = _ir_request.get_values()
                bot, top = vals[0].value, vals[1].value
            else:
                bot, top = gpio_read(PIN_IR_BOTTOM), gpio_read(PIN_IR_TOP)
            
            if bot == 0 or top == 0:
                ir_cooldown = now + 6.0; beep_now(1)
                threading.Thread(target=process_bottle, daemon=True).start()
        
        if _ir_reset: ir_cooldown = now + 1.0; _ir_reset = False
        time.sleep(0.01)

def display_manager():
    while True:
        if not bottle_lock.locked():
            s = session_data["state"]
            if s == "IDLE":
                lcd_write_force(["     ECO VENDO      ", "    PRESS START     ", "      TO BEGIN      ", "                    "])
            elif s == "INSERTING":
                cnt = session_data['count']
                lcd_write_force(["   INSERT BOTTLE    ", f"   Bottles: {cnt}", f"   Time: {cnt*5}m", "   B3: Confirm      "])
        time.sleep(0.1)

# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════
app = Flask(__name__)

if __name__ == '__main__':
    # Initial GPIO setup
    [gpio_setup(p, "out", "0") for p in PINS_RELAYS + [PIN_BUZZER, PIN_SERVO]]
    [gpio_setup(p, "in") for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]]
    
    init_lcd()
    servo_init()
    
    # Ensure standby at start
    servo_goto(90, pulses=60)
    
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    
    print("System Ready. Check LCD.")
    app.run(host='0.0.0.0', port=80)
