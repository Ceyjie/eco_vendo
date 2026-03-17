import time, threading, os, subprocess, json, uuid, signal
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
PIN_IR_BOTTOM     = "6"
PIN_IR_TOP        = "1"
PIN_BUZZER        = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS       = ["3", "2", "67", "21"]
PIN_SERVO         = "10"
SLOT_NAMES        = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE           = "eco_database.json"
ADMIN_PASSWORD    = "1234"

# ═══════════════════════════════════════════════════════════
# BOTTLE WEIGHT RANGES (Updated)
# ═══════════════════════════════════════════════════════════
WEIGHT_MIN_VALID  =  8
WEIGHT_SMALL_MIN  =  9
WEIGHT_SMALL_MAX  = 16
WEIGHT_NATURE_MIN = 16.1
WEIGHT_NATURE_MAX = 21.9
WEIGHT_COKE_1L_MIN = 22
WEIGHT_COKE_1L_MAX = 27
WEIGHT_BIG_MIN    = 28
WEIGHT_BIG_MAX    = 45

# ═══════════════════════════════════════════════════════════
# SERVO LOGIC (Sysfs Only for Reliability)
# ═══════════════════════════════════════════════════════════
PULSE_MIN_MS = 0.5
PULSE_MAX_MS = 2.5
PERIOD_MS    = 20.0

def servo_init():
    path = f"/sys/class/gpio/gpio{PIN_SERVO}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(PIN_SERVO)
            time.sleep(0.5) 
        except: pass
    try:
        with open(f"{path}/direction", "w") as f: f.write("out")
    except: pass

def servo_set_pin(val):
    try:
        with open(f"/sys/class/gpio/gpio{PIN_SERVO}/value", "w") as f:
            f.write("1" if val else "0")
    except: pass

def servo_goto(deg, pulses=60):
    deg = max(0, min(180, deg))
    pulse_ms = PULSE_MIN_MS + (deg / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    pulse_s, period_s = pulse_ms / 1000.0, PERIOD_MS / 1000.0
    for _ in range(pulses):
        servo_set_pin(1); time.sleep(pulse_s)
        servo_set_pin(0); time.sleep(period_s - pulse_s)
    servo_set_pin(0)

# ═══════════════════════════════════════════════════════════
# BOTTLE CLASSIFICATION (Updated)
# ═══════════════════════════════════════════════════════════
def classify_bottle(weight_g):
    if weight_g < WEIGHT_MIN_VALID:
        return 0, None, "   NOT A BOTTLE!    ", "   Bottles only!    "
    
    if WEIGHT_SMALL_MIN <= weight_g <= WEIGHT_SMALL_MAX:
        return 1, "SMALL", "  SMALL BOTTLE      ", "      +1 POINT!      "
    
    if WEIGHT_NATURE_MIN <= weight_g <= WEIGHT_NATURE_MAX:
        return 2, "NATURE", "  NATURE SPRING     ", "      +2 POINTS!     "
    
    if WEIGHT_COKE_1L_MIN <= weight_g <= WEIGHT_COKE_1L_MAX:
        return 2, "1L COKE/ROYAL", "  1L COKE/ROYAL     ", "      +2 POINTS!     "
    
    if WEIGHT_BIG_MIN <= weight_g <= WEIGHT_BIG_MAX:
        return 2, "LARGE", "  LARGE BOTTLE      ", "      +2 POINTS!     "
    
    return 0, None, "  INVALID BOTTLE!   ", " Weight mismatch    "

# ═══════════════════════════════════════════════════════════
# SYSTEM STATE & DATABASE
# ═══════════════════════════════════════════════════════════
session_data = {"state": "IDLE", "count": 0, "active_user": None, "selected_slot": 0}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}
bottle_lock = threading.Lock()
lcd_lock = threading.Lock()
_ir_reset = False

def load_db():
    try:
        with open(DB_FILE, 'r') as f: return json.load(f)
    except: return {"total_bottles": 0, "users": {}, "logs": []}

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# ═══════════════════════════════════════════════════════════
# GPIO HELPERS
# ═══════════════════════════════════════════════════════════
def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    try:
        with open(f"{path}/direction", "w") as f: f.write(direction)
        if direction == "out":
            with open(f"{path}/value", "w") as f: f.write(value)
    except: pass

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

# ═══════════════════════════════════════════════════════════
# HARDWARE LOOPS
# ═══════════════════════════════════════════════════════════
lcd = None
current_lcd_lines = ["", "", "", ""]

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear(); return
        except: pass

def lcd_write(new_lines):
    global current_lcd_lines, lcd
    if not lcd: init_lcd(); return
    with lcd_lock:
        try:
            safe = [str(line).ljust(20)[:20] for line in new_lines]
            for i, line in enumerate(safe):
                if line != current_lcd_lines[i]:
                    lcd.cursor_pos = (i, 0); lcd.write_string(line)
                    current_lcd_lines[i] = line
        except: lcd = None

def hx_get_grams():
    try:
        with open("/tmp/eco_weight.json", "r") as f:
            data = json.load(f)
            return data.get("grams", 0.0) if time.time() - data.get("ts", 0) < 1.5 else None
    except: return None

def process_bottle():
    global _ir_reset
    with bottle_lock:
        lcd_write(["    BOTTLE SENSED   ", "    Checking...     ", "  Place on sensor   ", "  Hold still...     "])
        time.sleep(1.2)
        samples = []
        for _ in range(12):
            w = hx_get_grams()
            if w is not None and w > WEIGHT_MIN_VALID: samples.append(w)
            time.sleep(0.3)
        
        if not samples:
            lcd_write(["    NO BOTTLE       ", "    DETECTED!       ", "  Place bottle on   ", "  sensor and retry  "])
            beep_now(3); time.sleep(2); _ir_reset = True; return

        weight = sorted(samples)[len(samples)//2]
        pts, label, l1, l2 = classify_bottle(weight)

        if pts > 0:
            lcd_write(["    ACCEPTED!       ", l1, l2, "    Processing...    "])
            servo_goto(0) # Open
            time.sleep(2.0)
            servo_goto(90) # Close
            session_data["count"] += pts
            db = load_db()
            db["total_bottles"] += 1
            db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label])
            save_db(db)
            beep_now(pts)
        else:
            lcd_write(["    INVALID ITEM!   ", l1, l2, "    Try again...     "])
            beep_now(2); time.sleep(2)
        _ir_reset = True

# [Standard hardware_loop, handle_physical_press, and Flask app remains identical to main81.py...]
# [Shortened for brevity - ensure your existing Flask and Loop code is pasted below this line]

def hardware_loop():
    global _ir_reset
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}
    while True:
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_val[p] == 1:
                handle_physical_press(p)
            last_val[p] = val
        time.sleep(0.05)

def handle_physical_press(pin):
    s = session_data["state"]
    if pin == PIN_BTN_START and s == "IDLE":
        beep_now(1); session_data.update({"state": "INSERTING", "count": 0})
    elif pin == PIN_BTN_CONFIRM and s == "INSERTING":
        session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        beep_now(1)
    # [Add your other button handlers here]

app = Flask(__name__)
@app.route('/')
def index():
    db = load_db()
    return render_template('index.html', points=0, logs=db["logs"][-5:])

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    gpio_setup(PIN_BUZZER, "out", "0")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0")
    for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    init_lcd()
    servo_init()
    servo_goto(90)
    threading.Thread(target=hardware_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
