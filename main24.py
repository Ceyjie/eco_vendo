import time, threading, os, subprocess, json, uuid, signal, queue, mmap, struct
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory, render_template_string
from RPLCD.i2c import CharLCD

app = Flask(__name__)

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
PIN_SERVO = "10"
DOUT_PIN, SCK_PIN = "68", "71"

CALIBRATION_FACTOR = 441.17
DB_FILE = "eco_database.json"

# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE",
    "count": 0,
    "active_user": None,
    "selected_slot": 0,
    "last_activity": time.time()
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}
cmd_queue = queue.Queue(maxsize=1)
tare_offset = 0

# --- 1. GPIO ENGINE (DEFINED FIRST) ---
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

def beep(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.08); gpio_write(PIN_BUZZER, 0); time.sleep(0.04)

# --- 2. SERVO ENGINE ---
def servo_worker():
    pulse_ms = 1.5  # Neutral 90
    while True:
        try: pulse_ms = cmd_queue.get_nowait()
        except: pass
        gpio_write(PIN_SERVO, 1)
        time.sleep(pulse_ms / 1000.0)
        gpio_write(PIN_SERVO, 0)
        time.sleep(0.02 - (pulse_ms / 1000.0))

def set_angle(angle):
    # 0 deg = 0.5ms, 90 deg = 1.5ms, 180 deg = 2.5ms
    pulse = 0.5 + (angle / 180.0) * 2.0
    try: cmd_queue.get_nowait()
    except: pass
    cmd_queue.put(pulse)

# --- 3. LOAD CELL (HX711) ENGINE ---
def read_weight():
    raw = 0
    # Wait for ready (max 0.5s)
    for _ in range(500):
        if gpio_read(DOUT_PIN) == 0: break
        time.sleep(0.001)
    else: return 0.0
    
    for _ in range(24):
        gpio_write(SCK_PIN, 1)
        raw = (raw << 1) | gpio_read(DOUT_PIN)
        gpio_write(SCK_PIN, 0)
    gpio_write(SCK_PIN, 1); gpio_write(SCK_PIN, 0)
    if raw & 0x800000: raw -= 0x1000000
    return abs((raw - tare_offset) / CALIBRATION_FACTOR)

# --- 4. LCD ENGINE ---
lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4)

def lcd_write(lines):
    lcd.clear()
    for i, line in enumerate(lines):
        lcd.cursor_pos = (i, 0)
        lcd.write_string(line[:20])

# --- 5. VERIFICATION LOGIC ---
def process_bottle():
    lcd_write(["   VERIFYING...", " KEEP BOTTLE STILL", "  CHECKING WEIGHT", "  PLEASE WAIT..."])
    time.sleep(1.0) # Let scale settle
    
    weight = read_weight()
    bot = gpio_read(PIN_IR_BOTTOM)
    top = gpio_read(PIN_IR_TOP)
    
    points = 0
    # Logic: Big (30g avg), Nature Spring (17g avg), Small (14g avg)
    if bot == 0 and top == 0 and 29 <= weight <= 35:
        points = 2 # Big Bottle
    elif bot == 0 and top == 0 and 16 <= weight <= 19:
        points = 2 # Nature Spring
    elif bot == 0 and top == 1 and 12 <= weight <= 16:
        points = 1 # Small Bottle

    if points > 0:
        beep(1)
        lcd_write(["   VERIFIED!", f"   POINTS: +{points}", "   OPENING...", "  PLEASE WAIT..."])
        set_angle(0)     # Move to 0 to drop
        time.sleep(3.0)  # Wait for removal/drop
        session_data["count"] += points
        set_angle(90)    # Return to 90
        # Return to Insert Screen
        lcd_write(["   INSERT BOTTLE", f"   BOTTLES: {session_data['count']}", f"   TIME: {session_data['count']*5}m", "B3:CONFIRM"])
    else:
        beep(3)
        lcd_write(["  INVALID OBJECT", "  BOTTLES ONLY!", " PLEASE REMOVE", " AND TRY AGAIN"])
        time.sleep(3.0)
        lcd_write(["   INSERT BOTTLE", f"   BOTTLES: {session_data['count']}", f"   TIME: {session_data['count']*5}m", "B3:CONFIRM"])

# --- 6. BACKGROUND LOOPS ---
def hardware_loop():
    global tare_offset
    # Simple Tare
    r = read_weight()
    tare_offset = r if r else 0

    while True:
        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0:
                process_bottle()
                session_data["last_activity"] = time.time()
        time.sleep(0.1)

# --- 7. FLASK & MAIN ---
@app.route('/')
def index():
    return "Eco Vendo Active"

if __name__ == '__main__':
    # 1. SETUP ALL PINS
    gpio_setup(PIN_SERVO, "out")
    gpio_setup(PIN_BUZZER, "out")
    gpio_setup(SCK_PIN, "out")
    gpio_setup(DOUT_PIN, "in")
    gpio_setup(PIN_IR_BOTTOM, "in")
    gpio_setup(PIN_IR_TOP, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out")

    # 2. START POSITION
    set_angle(90)
    
    # 3. START THREADS
    threading.Thread(target=servo_worker, daemon=True).start()
    threading.Thread(target=hardware_loop, daemon=True).start()
    
    app.run(host='0.0.0.0', port=80)
