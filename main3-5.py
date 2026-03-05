import time, threading, os, subprocess, sqlite3, uuid
from flask import Flask, render_template, jsonify, session, redirect
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"   # Button 1: Start
PIN_BTN_SELECT  = "14"   # Button 2: Select Slot
PIN_BTN_CONFIRM = "110"  # Button 3: Confirm/Enter
PINS_RELAYS = ["3", "2", "67", "21"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"active": False, "count": 0}
slot_status  = [0, 0, 0, 0]
ui_state     = {"state": "IDLE", "selected_slot": 0}
lcd_lock     = threading.Lock()

# --- LCD SETUP ---
try:
    lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4, charmap='A00')
except:
    try: lcd = CharLCD('PCF8574', 0x3f, port=0, cols=20, rows=4, charmap='A00')
    except: lcd = None

def lcd_write_safe(lines):
    with lcd_lock:
        if not lcd: return
        try:
            lcd.clear()
            for i, line in enumerate(lines[:4]):
                lcd.cursor_pos = (i, 0)
                lcd.write_string(line[:20])
        except: pass

# --- GPIO HELPERS ---
def gpio_setup(pin, direction="in", value="1"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: return
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"{path}/value", "w") as f: f.write(value)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# --- CORE LOGIC ---
def run_relay(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 0) # ON
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 1) # OFF
    slot_status[slot] = 0

def on_btn_start():
    ui_state["state"] = "INSERTING"
    session_data["active"] = True
    session_data["count"] = 0
    gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)

def on_btn_select():
    # Loop to find next available slot (skips busy ones)
    for _ in range(4):
        ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
        if slot_status[ui_state["selected_slot"]] == 0:
            break
    gpio_write(PIN_BUZZER, 1); time.sleep(0.05); gpio_write(PIN_BUZZER, 0)

def on_btn_confirm():
    if ui_state["state"] == "INSERTING" and session_data["count"] > 0:
        slot = ui_state["selected_slot"]
        pts = session_data["count"]
        duration = pts * 300 # 5 mins per point
        
        # Start the timer in background
        threading.Thread(target=run_relay, args=(slot, duration), daemon=True).start()
        
        ui_state["state"] = "IDLE"
        session_data["active"] = False
        lcd_write_safe(["  CHARGING START", f" SLOT: {slot+1}", f" TIME: {pts*5} MINS", "  THANK YOU!"])
        time.sleep(3)

# --- LOOPS ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        # Buttons
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start()
                elif p == PIN_BTN_SELECT: on_btn_select()
                elif p == PIN_BTN_CONFIRM: on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        # IR Sensors
        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            s = ui_state["selected_slot"]
            t1 = f"{'>' if s==0 else ' '}U1:{slot_status[0]//60}m  {'>' if s==1 else ' '}U2:{slot_status[1]//60}m"
            t2 = f"{'>' if s==2 else ' '}U3:{slot_status[2]//60}m  {'>' if s==3 else ' '}AC:{slot_status[3]//60}m"
            lcd_write_safe(["   ECO-CHARGE VENDO", "  SELECT PORT [BT2]", t1, t2])
        elif ui_state["state"] == "INSERTING":
            pts = session_data["count"]
            lcd_write_safe(["   INSERT BOTTLE", f" COUNT: {pts}", f" TIME: {pts*5} MINS", " [BT3] TO CONFIRM"])
        time.sleep(0.5)

# --- FLASK ---
app = Flask(__name__)
app.secret_key = "eco_local_99"

@app.route('/')
def index():
    return render_template('index.html', points=session_data["count"])

@app.route('/api/status')
def get_status():
    return jsonify({
        "session": session_data["count"],
        "slots": slot_status,
        "state": ui_state["state"],
        "selected": ui_state["selected_slot"]
    })

@app.route('/api/start_session')
def start_api():
    on_btn_start()
    return jsonify({"status": "ok"})

@app.route('/api/stop_session')
def stop_api():
    on_btn_confirm()
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    subprocess.run("sudo fuser -k 80/tcp", shell=True, capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
