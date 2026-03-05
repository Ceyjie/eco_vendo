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
ui_state     = {"state": "IDLE", "selected_slot": 0}

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
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins}:{secs:02d}"

# --- EXIT HANDLER (LCD OFF) ---
def close_app(sig, frame):
    print("\nShutting down...")
    if lcd:
        lcd.clear()
        lcd.backlight_enabled = False
    for p in PINS_RELAYS: gpio_write(p, 1)
    sys.exit(0)

signal.signal(signal.SIGINT, close_app)

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
    if ui_state["state"] in ["IDLE", "DONE"]:
        ui_state["state"] = "INSERTING"
        session_data.update({"active": True, "count": 0})
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
        lcd_write(["   INSERT BOTTLE", "", " TOTAL BOTTLES: 0", " [CONFIRM] to Save"])

def on_btn_select():
    if ui_state["state"] == "SELECTING":
        ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
        slot = ui_state["selected_slot"]
        lcd_write([" SELECT OUTPUT:", f" > {SLOT_NAMES[slot]}", " [CONFIRM] to start", " [SELECT] to cycle"])

def on_btn_confirm():
    if ui_state["state"] == "INSERTING":
        ui_state["state"] = "SELECTING"
        session_data["active"] = False
        ui_state["selected_slot"] = 0
        lcd_write([" BOTTLES SAVED!", " Select Slot...", f" > {SLOT_NAMES[0]}", " [SELECT] to cycle"])

	elif ui_state["state"] == "SELECTING":
        slot = ui_state["selected_slot"]
        # Use the count from the session
        total_minutes = session_data["count"] * 5
        charge_seconds = session_data["count"] * 300
        
        threading.Thread(target=run_relay_timer, args=(slot, charge_seconds), daemon=True).start()
        
        ui_state["state"] = "DONE"
        lcd_write([
            "   ACTIVATED!", 
            f" Slot: {SLOT_NAMES[slot]}", 
            f" Time: {total_minutes} mins", 
            " Returning..."
        ])
        time.sleep(3)
        ui_state["state"] = "IDLE"


# --- LOOPS ---
def display_manager():
    """Handles the live timers on the IDLE screen"""
    while True:
        if ui_state["state"] == "IDLE":
            timer_row3 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            timer_row4 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["     ECO VENDO", "   PRESS START", timer_row3, timer_row4])
        time.sleep(1)

def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start()
                elif p == PIN_BTN_SELECT: on_btn_select()
                elif p == PIN_BTN_CONFIRM: on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                lcd_write(["   INSERT BOTTLE", "", f" TOTAL BOTTLES: {session_data['count']}", " [CONFIRM] to Save"])
                time.sleep(0.7)
        time.sleep(0.05)

# --- FLASK ---
app = Flask(__name__)


# ... [Keep your GPIO and Hardware code from previous main3.py] ...

@app.route('/')
def index():
    # You'll need to handle device_id and points via session/cookies or DB
    # For now, we'll pass dummy data so the page loads
    return render_template('index.html', device_id="ORANGE-PI-01", points=session_data["count"], logs=[])

@app.route('/api/start_session')
def start_api():
    on_btn_start() # Trigger the hardware start logic
    return jsonify({"status": "started"})

@app.route('/api/stop_session')
def stop_api():
    on_btn_confirm() # Trigger the hardware save logic
    return jsonify({"status": "stopped"})

@app.route('/api/status')
def get_status():
    # We return data in the format your Javascript expects
    return jsonify({
        "session": session_data["count"],
        "slots": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]],
        "state": ui_state["state"]
    })

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_web(slot, pts):
    # Logic: 1 point (bottle) = 300 seconds (5 minutes)
    total_seconds = pts * 300 
    
    if slot_status[slot] > 0:
        return "Slot is already busy!", 400
    
    # Start the timer in the background
    threading.Thread(target=run_relay_timer, args=(slot, total_seconds), daemon=True).start()
    
    # Redirect back to the home page after starting
    return redirect('/')




# ... [Keep the rest of the file] ...




if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "5000/tcp"], capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
