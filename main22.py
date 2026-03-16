import time, threading, os, subprocess, json, uuid, signal, mmap, struct, queue
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory, render_template_string
from RPLCD.i2c import CharLCD

app = Flask(__name__)

# --- CONFIGURATION ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
PIN_SERVO = "10"
DOUT_PIN, SCK_PIN = "68", "71"

# Weight Calibration
CALIBRATION_FACTOR = 441.17 
DB_FILE = "eco_database.json"

# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE",
    "count": 0,
    "active_user": None,
    "selected_slot": 0,
    "last_activity": time.time(),
    "current_weight": 0.0
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}
cmd_queue = queue.Queue(maxsize=1)

# --- GPIO & HX711 ENGINE ---
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

# --- HX711 FUNCTIONS ---
tare_offset = 0
def read_raw_hx711():
    # Basic HX711 bit-banging
    raw = 0
    # Wait for ready
    for _ in range(100):
        if gpio_read(DOUT_PIN) == 0: break
        time.sleep(0.001)
    else: return None
    
    for _ in range(24):
        gpio_write(SCK_PIN, 1)
        raw = (raw << 1) | gpio_read(DOUT_PIN)
        gpio_write(SCK_PIN, 0)
    gpio_write(SCK_PIN, 1); gpio_write(SCK_PIN, 0)
    if raw & 0x800000: raw -= 0x1000000
    return raw

# --- SERVO ENGINE ---
def servo_worker():
    pulse_ms = 1.5 # 90 degrees
    while True:
        try: pulse_ms = cmd_queue.get_nowait()
        except: pass
        
        # PWM bit-bang
        gpio_write(PIN_SERVO, 1)
        time.sleep(pulse_ms / 1000.0)
        gpio_write(PIN_SERVO, 0)
        time.sleep(0.02 - (pulse_ms / 1000.0))

def set_servo_angle(angle):
    pulse = 0.5 + (angle / 180.0) * 2.0
    try: cmd_queue.get_nowait()
    except: pass
    cmd_queue.put(pulse)

# --- LCD ENGINE ---
lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4)

def lcd_write(lines):
    lcd.clear()
    for i, line in enumerate(lines):
        lcd.cursor_pos = (i, 0)
        lcd.write_string(line[:20])

# --- VERIFICATION LOGIC ---
def verify_and_drop():
    bot = gpio_read(PIN_IR_BOTTOM)
    top = gpio_read(PIN_IR_TOP)
    raw_w = read_raw_hx711()
    if raw_w is None: return
    weight = abs((raw_w - tare_offset) / CALIBRATION_FACTOR)
    
    points = 0
    msg = ""

    # Logic Tree
    if bot == 0 and top == 1 and 12 <= weight <= 16:
        points = 1
        msg = "SMALL BOTTLE"
    elif bot == 0 and top == 0 and 16 <= weight <= 19:
        points = 2
        msg = "NATURE SPRING"
    elif bot == 0 and top == 0 and 29 <= weight <= 35:
        points = 2
        msg = "BIG BOTTLE"
    
    if points > 0:
        lcd_write(["   VERIFIED!    ", msg, "  PLEASE WAIT... ", f" POINTS: +{points}"])
        session_data["count"] += points
        set_servo_angle(0) # Open
        time.sleep(2.5)    # Wait for drop
        set_servo_angle(90)# Close
    else:
        lcd_write([" INVALID OBJECT ", "  BOTTLES ONLY  ", " PLEASE REMOVE  ", "   AND TRY AGAIN"])
        time.sleep(3)

# --- BACKGROUND THREADS ---
def hardware_loop():
    global tare_offset
    # Initial Tare
    r = read_raw_hx711()
    if r: tare_offset = r

    while True:
        if session_data["state"] == "INSERTING":
            # If weight or IR detected
            if gpio_read(PIN_IR_BOTTOM) == 0:
                verify_and_drop()
                session_data["last_activity"] = time.time()
        time.sleep(0.1)

# --- FLASK ROUTES (Abbreviated) ---
@app.route('/')
def index():
    return "Eco Vendo Online. Use Physical Buttons or Web to Start."

@app.route('/api/start')
def start():
    session_data["state"] = "INSERTING"
    return jsonify({"status":"started"})

if __name__ == '__main__':
    # Setup all pins
    gpio_setup(PIN_SERVO, "out")
    gpio_setup(PIN_IR_BOTTOM, "in")
    gpio_setup(PIN_IR_TOP, "in")
    gpio_setup(SCK_PIN, "out")
    gpio_setup(DOUT_PIN, "in")
    
    set_servo_angle(90) # Lock door
    
    threading.Thread(target=servo_worker, daemon=True).start()
    threading.Thread(target=hardware_loop, daemon=True).start()
    
    app.run(host='0.0.0.0', port=80)

