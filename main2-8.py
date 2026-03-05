import time, threading, os, subprocess, signal, sys
from flask import Flask, render_template, request, jsonify, redirect
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"
PIN_BTN_SELECT  = "14"
PIN_BTN_CONFIRM = "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# --- STATE ---
session_data = {"active": False, "count": 0}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE"}

# --- GPIO HELPERS ---
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
        time.sleep(0.02)
        for i, line in enumerate(lines[:4]):
            lcd.cursor_pos = (i, 0)
            lcd.write_string(line[:20])
    except: pass

def format_time(seconds):
    return f"{seconds // 60}:{seconds % 60:02d}"

# --- LOGIC ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 0)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 1)
    slot_status[slot] = 0

def on_btn_start():
    if ui_state["state"] == "IDLE":
        ui_state["state"] = "INSERTING"
        session_data["active"] = True
        session_data["count"] = 0
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
        lcd_write(["   INSERT BOTTLE", "", " BOTTLES: 0", " [DONE] via App"])

def on_btn_confirm():
    if ui_state["state"] == "INSERTING":
        ui_state["state"] = "IDLE"
        session_data["active"] = False
        lcd_write(["   POINTS READY", f" Total: {session_data['count']}", " Select on App", ""])
        time.sleep(2)

# --- LOOPS ---
def hardware_loop():
    last_start = 1
    while True:
        # Check physical Start Button
        val = gpio_read(PIN_BTN_START)
        if val == 0 and last_start == 1:
            on_btn_start()
            time.sleep(0.2)
        last_start = val

        # Sensor logic
        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                lcd_write(["   INSERT BOTTLE", "", f" BOTTLES: {session_data['count']}", " [DONE] via App"])
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "   READY TO SCAN", t1, t2])
        time.sleep(1)

# --- FLASK ---
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html', device_id="LOCAL-MODE", points=session_data["count"])

@app.route('/api/status')
def get_status():
    return jsonify({
        "session": session_data["count"],
        "slots": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]],
        "state": ui_state["state"]
    })

@app.route('/api/start_session')
def start_api():
    on_btn_start()
    return jsonify({"status": "started"})

@app.route('/api/stop_session')
def stop_api():
    on_btn_confirm()
    return jsonify({"status": "stopped"})

@app.route('/redeem/<int:slot>')
def redeem_all(slot):
    pts = session_data["count"]
    if pts <= 0: return redirect('/')
    
    # Calculation: All points * 300 seconds (5 mins)
    total_seconds = pts * 300
    
    if slot_status[slot] == 0:
        threading.Thread(target=run_relay_timer, args=(slot, total_seconds), daemon=True).start()
        session_data["count"] = 0 # Reset points after use
        
    return redirect('/')

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
