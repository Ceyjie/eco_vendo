import time, threading, os, json, uuid, subprocess
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "eco_database.json"

# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE", 
    "count": 0, 
    "active_user": None,
    "selected_slot": 0,
    "add_time_choice": 1, # 1 for Yes, 0 for No
    "last_activity": time.time(),
    "sensor_lockout_until": 0
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- DATABASE ENGINE ---
def load_db():
    if not os.path.exists(DB_FILE): return {"total_bottles": 0, "users": {}, "logs": []}
    try:
        with open(DB_FILE, 'r') as f: return json.load(f)
    except: return {"total_bottles": 0, "users": {}, "logs": []}

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# --- GPIO HELPERS ---
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
    def run():
        for _ in range(times):
            gpio_write(PIN_BUZZER, 1); time.sleep(0.08); gpio_write(PIN_BUZZER, 0); time.sleep(0.04)
    threading.Thread(target=run, daemon=True).start()

# --- LCD ENGINE ---
lcd = None
current_lcd_lines = ["", "", "", ""]

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear()
            return
        except: lcd = None

def lcd_write(new_lines):
    global current_lcd_lines
    if not lcd: return
    try:
        new_lines = [line.ljust(20)[:20] for line in new_lines]
        for i, line in enumerate(new_lines):
            if line != current_lcd_lines[i]:
                lcd.cursor_pos = (i, 0)
                lcd.write_string(line)
                current_lcd_lines[i] = line
    except: init_lcd()

def format_time(seconds):
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

# --- RELAY ENGINE ---
def start_or_extend_relay(slot, additional_seconds):
    is_running = slot_status[slot] > 0
    slot_status[slot] += additional_seconds
    if not is_running:
        threading.Thread(target=relay_worker, args=(slot,), daemon=True).start()

def relay_worker(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1)
        if slot_status[slot] > 0: slot_status[slot] -= 1
        else: break
    gpio_write(PINS_RELAYS[slot], 0)

# --- HARDWARE STEP-BY-STEP LOGIC ---
def hardware_loop():
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_btn_state = {p: 1 for p in btn_pins}
    
    while True:
        now = time.time()

        # 1. BUTTON POLLING (Always Active)
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_btn_state[p] == 1:
                if (now - session_data["last_activity"]) > 0.15: # Snappy debounce
                    handle_physical_press(p)
            last_btn_state[p] = val

        # 2. SENSOR LOGIC (Only during INSERTING)
        if session_data["state"] == "INSERTING" and now > session_data["sensor_lockout_until"]:
            if gpio_read(PIN_IR_BOTTOM) == 0:
                # Tall vs Small Bottle check
                points = 2 if gpio_read(PIN_IR_TOP) == 0 else 1
                session_data["count"] += points
                session_data["last_activity"] = now
                session_data["sensor_lockout_until"] = now + 1.0 # 1s Lockout
                beep(1)

        # 3. AUTO-TIMEOUT (Return to IDLE if inactive for 60s)
        if session_data["state"] not in ["IDLE", "THANK_YOU"]:
            if (now - session_data["last_activity"]) > 60:
                session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                beep(3)

        time.sleep(0.01)

def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]

    # STEP 1: START (From IDLE)
    if pin == PIN_BTN_START and s == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL"})
        beep(1)

    # STEP 2: CONFIRM INSERTION -> GO TO SELECTING
    elif pin == PIN_BTN_CONFIRM and s == "INSERTING":
        if session_data["count"] > 0:
            session_data["state"] = "SELECTING"
            beep(1)
        else:
            session_data["state"] = "IDLE" # No bottles? Just exit.
            beep(2)

    # STEP 3: SELECT SLOT (Navigation)
    elif pin == PIN_BTN_SELECT:
        if s == "SELECTING":
            session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
            beep(1)
        elif s == "ADD_TIME_PROMPT":
            session_data["add_time_choice"] = 1 - session_data["add_time_choice"]
            beep(1)

    # STEP 4: CONFIRM SELECTION -> CHECK IF RUNNING OR FINALIZE
    elif pin == PIN_BTN_CONFIRM and s == "SELECTING":
        if slot_status[session_data["selected_slot"]] > 0:
            session_data["state"] = "ADD_TIME_PROMPT"
        else:
            finalize_transaction()
        beep(1)

    # STEP 5: FINAL CONFIRM (ADD TIME PROMPT)
    elif pin == PIN_BTN_CONFIRM and s == "ADD_TIME_PROMPT":
        if session_data["add_time_choice"] == 1:
            finalize_transaction()
        else:
            session_data["state"] = "SELECTING"
        beep(1)

def finalize_transaction():
    slot = session_data["selected_slot"]
    pts = int(session_data["count"])
    
    # Save to DB
    db = load_db()
    uid = "LOCAL_USER"
    if uid not in db["users"]: db["users"][uid] = {"points": 0}
    db["users"][uid]["points"] += pts
    db["total_bottles"] += 1
    db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", SLOT_NAMES[slot]])
    save_db(db)

    # Start Relay (1 Pt = 300 Seconds)
    start_or_extend_relay(slot, pts * 300)
    
    # Show Thank You
    session_data["state"] = "THANK_YOU"
    beep(2)
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0, "active_user": None})).start()

# --- LCD MANAGER ---
def display_manager():
    while True:
        s = session_data["state"]
        if s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "     PRESS START", t1, t2])
        elif s == "INSERTING":
            lcd_write(["   INSERT BOTTLE", f"    POINTS: {session_data['count']}", f"    TIME: {session_data['count']*5}m", "B3: CONFIRM"])
        elif s == "SELECTING":
            lcd_write(["   SELECT SLOT", f"   > {SLOT_NAMES[session_data['selected_slot']]}", f"   PTS: {session_data['count']}", "B2:NEXT B3:OK"])
        elif s == "ADD_TIME_PROMPT":
            ch = "> YES    NO " if session_data["add_time_choice"] == 1 else "  YES > NO "
            lcd_write(["   ADD MINUTES TO", f"   {SLOT_NAMES[session_data['selected_slot']]}?", ch, "B2:MOVE B3:OK"])
        elif s == "THANK_YOU":
            lcd_write(["      THANK YOU!", "   POINTS APPLIED", "   HAVE A NICE DAY", "--------------------"])
        time.sleep(0.1)

# --- FLASK ---
app = Flask(__name__)

@app.route('/')
def index():
    db = load_db(); uid = request.cookies.get('user_uuid') or str(uuid.uuid4())[:8]
    if uid not in db["users"]: db["users"][uid] = {"points": 0}; save_db(db)
    resp = make_response(render_template('index.html', device_id=uid, points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80, debug=False)
