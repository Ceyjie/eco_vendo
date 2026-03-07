import time, threading, os, subprocess
from flask import Flask, jsonify, render_template
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# --- STATE ---
session_data = {
    "state": "IDLE", 
    "count": 0, 
    "selected_slot": 0,
    "last_activity": time.time()
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- GPIO HELPERS ---
def gpio_setup(pin, direction="in", value="0"):
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
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

def beep(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0); time.sleep(0.05)

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

# --- RELAY TIMER (UPDATED FOR ACTIVE HIGH) ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 1) # ON (Active High)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 0) # OFF (Active High)
    slot_status[slot] = 0

# --- BUTTON LOGIC ---
def reset_activity():
    session_data["last_activity"] = time.time()

def on_btn_start():
    if session_data["state"] == "IDLE":
        beep(1)
        session_data.update({"count": 0, "state": "INSERTING"})
        reset_activity()

def on_btn_select():
    if session_data["state"] == "SELECTING":
        beep(1)
        session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        reset_activity()

def on_btn_confirm():
    reset_activity()
    if session_data["state"] == "INSERTING":
        if session_data["count"] > 0:
            beep(1)
            session_data["state"] = "SELECTING"
        else:
            beep(1)
            session_data["state"] = "IDLE"
            
    elif session_data["state"] == "SELECTING":
        slot = session_data["selected_slot"]
        total_seconds = session_data["count"] * 300 
        beep(2)
        threading.Thread(target=run_relay_timer, args=(slot, total_seconds), daemon=True).start()
        lcd_write(["   ACTIVATING...", f"   {SLOT_NAMES[slot]}", f"   {session_data['count']*5} MINS TOTAL", "   USE IT NOW!"])
        session_data["state"] = "IDLE"
        session_data["count"] = 0
        time.sleep(3)

# --- LOOPS ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        # Check Buttons
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start()
                elif p == PIN_BTN_SELECT: on_btn_select()
                elif p == PIN_BTN_CONFIRM: on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        # Check Bottles
        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                beep(1)
                reset_activity()
                time.sleep(0.7)
        
        # Auto-Reset Timeout (60 Seconds)
        if session_data["state"] != "IDLE":
            if time.time() - session_data["last_activity"] > 60:
                session_data["state"] = "IDLE"
                session_data["count"] = 0
                beep(3) 

        time.sleep(0.05)

def display_manager():
    last_state, last_count, last_slot = "", -1, -1
    while True:
        s, c, sl = session_data["state"], session_data["count"], session_data["selected_slot"]
        if s != last_state or c != last_count or sl != last_slot:
            if s == "IDLE":
                t1 = f"USB1:{slot_status[0]//60:02d}m USB2:{slot_status[1]//60:02d}m"
                t2 = f"USB3:{slot_status[2]//60:02d}m  AC:{slot_status[3]//60:02d}m"
                lcd_write(["      ECO VENDO", "     PRESS START", t1, t2])
            elif s == "INSERTING":
                lcd_write(["   INSERT BOTTLE", f"   BOTTLES: {c}", f"   TIME: {c*5}m", "B3:CONFIRM"])
            elif s == "SELECTING":
                lcd_write(["      SELECT", f"    > {SLOT_NAMES[sl]}", f"    FOR {c*5} MINS", "B3:CONFIRM"])
            last_state, last_count, last_slot = s, c, sl
        time.sleep(0.2)

# --- FLASK ---
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    return jsonify({
        "state": session_data["state"],
        "count": session_data["count"],
        "timers": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]]
    })

@app.route('/api/web_start')
def web_start():
    on_btn_start()
    return jsonify({"status": "ok"})

@app.route('/api/web_confirm')
def web_confirm():
    on_btn_confirm()
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    # Kill any existing process on port 80
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    
    # Setup Inputs
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: 
        gpio_setup(p, "in")
    
    # Setup Outputs (Relays initialized to 0 for Active High)
    for p in PINS_RELAYS: 
        gpio_setup(p, "out", "0") 
    
    gpio_setup(PIN_BUZZER, "out", "0")
    
    # Start Background Threads
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    
    # Run Web Server
    app.run(host='0.0.0.0', port=80)
