import sqlite3, uuid, time, threading, os, subprocess
from flask import Flask, render_template, request, jsonify, make_response
from RPLCD.i2c import CharLCD

# --- CONFIG (Working Kernel IDs) ---
PIN_IR_BOTTOM = "6"     # PA6
PIN_IR_TOP    = "1"     # PA1
PIN_BUZZER    = "0"     # PA0
PIN_BTN_START   = "13"  # PA13
PIN_BTN_SELECT  = "14"  # PA14
PIN_BTN_CONFIRM = "110" # PD14
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# --- STATE ---
session_data = {"active": False, "count": 0}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0} # Holds remaining seconds
ui_state     = {"state": "IDLE", "selected_slot": 0, "uid": None}

# --- DIRECT KERNEL GPIO HELPERS ---
def gpio_setup(pin, direction="in", value="1"):
    if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: return
    time.sleep(0.1)
    with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(value)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# --- LCD ---
try:
    lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4, charmap='A00')
except:
    try: lcd = CharLCD('PCF8574', 0x3f, port=0, cols=20, rows=4, charmap='A00')
    except: lcd = None

def lcd_write(lines):
    if not lcd: return
    try:
        lcd.clear()
        time.sleep(0.05)
        for i, line in enumerate(lines[:4]):
            lcd.cursor_pos = (i, 0)
            lcd.write_string(line[:20])
    except: pass

# --- TIMER LOGIC ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 0) # Relay ON (Active Low)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 1) # Relay OFF
    slot_status[slot] = 0

# --- BUTTON ACTIONS ---
def on_btn_start():
    global ui_state
    if ui_state["state"] in ["IDLE", "DONE"]:
        ui_state["state"] = "INSERTING"
        session_data.update({"active": True, "count": 0})
        gpio_write(PIN_BUZZER, 1); time.sleep(0.2); gpio_write(PIN_BUZZER, 0)
        lcd_write(["   INSERT BOTTLE", "", "   Count: 0", " [CONFIRM] to save"])

def on_btn_select():
    if ui_state["state"] == "SELECTING":
        ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
        slot = ui_state["selected_slot"]
        lcd_write([" SELECT OUTPUT:", f" > {SLOT_NAMES[slot]}", " [CONFIRM] to start", " [SELECT] to cycle"])

def on_btn_confirm():
    global ui_state
    if ui_state["state"] == "INSERTING":
        ui_state["state"] = "SELECTING"
        session_data["active"] = False
        lcd_write([" BOTTLES SAVED!", " Select Slot...", f" > {SLOT_NAMES[0]}", " [CONFIRM] to use"])
    
    elif ui_state["state"] == "SELECTING":
        slot = ui_state["selected_slot"]
        if slot_status[slot] > 0:
            lcd_write([" SLOT IS BUSY!", " Wait for timer", "", " [SELECT] other"])
            return
        # Start 5 Minute Timer (300 seconds)
        threading.Thread(target=run_relay_timer, args=(slot, 300), daemon=True).start()
        ui_state["state"] = "DONE"
        lcd_write(["   CHARGING...", f" Slot: {SLOT_NAMES[slot]}", " Time: 5:00", " Press START"])

# --- BACKGROUND LOOPS ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        # Check Buttons
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start()
                if p == PIN_BTN_SELECT: on_btn_select()
                if p == PIN_BTN_CONFIRM: on_btn_confirm()
            last[p] = val
        
        # Check IR Sensors
        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                lcd_write(["   INSERT BOTTLE", "", f"   Count: {session_data['count']}", " [CONFIRM] to save"])
                time.sleep(0.7)
        
        time.sleep(0.05)

# --- FLASK ---
app = Flask(__name__)
@app.route('/')
def index():
    return render_template('index.html', count=session_data["count"])

@app.route('/api/status')
def get_status():
    return jsonify({"count": session_data["count"], "timers": slot_status})

if __name__ == '__main__':
    # 1. Clear Port
    subprocess.run(["sudo", "fuser", "-k", "5000/tcp"], capture_output=True)
    
    # 2. Init Hardware
    print("Initializing Sysfs...")
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
        gpio_setup(p, "in")
    for p in PINS_RELAYS:
        gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    
    # 3. Start LCD
    lcd_write(["  ECO-CHARGE VENDO", "--------------------", "   READY TO USE", "   Press START"])
    
    # 4. Start Thread & App
    threading.Thread(target=hardware_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
