import time, threading, os, subprocess, json
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- ORANGE PI ONE GPIO (Sysfs) ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"] 
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

DB_FILE = "eco_database.json"

# --- DATABASE ENGINE ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

def get_uid():
    # Priority 1: Check for Cookie ID
    uid = request.cookies.get('user_uuid')
    
    # Priority 2: Fallback to IP only for the very first contact 
    # (though we will overwrite this with a Cookie immediately)
    if not uid:
        uid = request.remote_addr
    return uid


# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE", 
    "count": 0, 
    "active_user": None, # Tracks WHO is currently inserting
    "selected_slot": 0,
    "add_time_choice": 1, 
    "last_activity": time.time()
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

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
    return f"{m:02d}:{s:02d}" if m < 60 else f"{m//60:02d}:{m%60:02d}"

# --- RELAY LOGIC (Supports adding time while running) ---
def start_or_extend_relay(slot, additional_seconds):
    is_already_running = slot_status[slot] > 0
    slot_status[slot] += additional_seconds
    
    if not is_already_running:
        threading.Thread(target=run_relay_thread, args=(slot,), daemon=True).start()

def run_relay_thread(slot):
    gpio_write(PINS_RELAYS[slot], 1) # RELAY ON
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] -= 1
    gpio_write(PINS_RELAYS[slot], 0) # RELAY OFF

# --- HARDWARE BUTTON HANDLERS ---
def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]

    if pin == PIN_BTN_START and s == "IDLE":
        beep(1)
        session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL_USER"})
    
    elif pin == PIN_BTN_SELECT:
        if s == "SELECTING":
            beep(1)
            session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        elif s == "ADD_TIME_PROMPT":
            beep(1)
            session_data["add_time_choice"] = 1 - session_data["add_time_choice"]

    elif pin == PIN_BTN_CONFIRM:
        if s == "INSERTING":
            beep(1)
            session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        elif s == "SELECTING":
            beep(1)
            # Physical users always get the "Add?" prompt if it's already running
            if slot_status[session_data["selected_slot"]] > 0:
                session_data["state"] = "ADD_TIME_PROMPT"
            else:
                finalize_transaction()
        elif s == "ADD_TIME_PROMPT":
            if session_data["add_time_choice"] == 1:
                finalize_transaction()
            else:
                beep(1)
                session_data["state"] = "SELECTING"

def finalize_transaction():
    slot = session_data["selected_slot"]
    pts = session_data["count"]
    beep(2)
    start_or_extend_relay(slot, pts * 300)
    
    session_data["state"] = "THANK_YOU"
    lcd_write(["     THANK YOU!     ", " You helped protect ", " our environment by ", " recycling plastic! "])
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0, "active_user": None})).start()

# --- BACKGROUND LOOPS ---
def hardware_loop():
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}
    last_press = {p: 0 for p in btn_pins}
    
    while True:
        now = time.time()
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_val[p] == 1:
                if (now - last_press[p]) > 0.05:
                    handle_physical_press(p)
                    last_press[p] = now
            last_val[p] = val

        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                session_data["last_activity"] = now
                beep(1)
                time.sleep(0.6)

        if session_data["state"] not in ["IDLE", "THANK_YOU"]:
            if (now - session_data["last_activity"]) > 60:
                session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                beep(3)
        time.sleep(0.01)

def display_manager():
    while True:
        s = session_data["state"]
        if s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "     PRESS START", t1, t2])
        elif s == "INSERTING":
            lcd_write(["   INSERT BOTTLE", f"   BOTTLES: {session_data['count']}", f"   TIME: {session_data['count']*5}m", "B3:CONFIRM"])
        elif s == "SELECTING":
            lcd_write(["      SELECT", f"    > {SLOT_NAMES[session_data['selected_slot']]}", f"    FOR {session_data['count']*5} MINS", "B3:CONFIRM"])
        elif s == "ADD_TIME_PROMPT":
            ch = "> YES   NO " if session_data["add_time_choice"] == 1 else "  YES > NO "
            lcd_write(["   ADD MINUTES TO", f"   {SLOT_NAMES[session_data['selected_slot']]}?", ch, "B2:MOVE B3:OK"])
        time.sleep(0.1)

# --- FLASK WEB SERVER ---
app = Flask(__name__)

def get_user_id():
    # Identifies the phone by its IP address or a unique cookie
    uid = request.remote_addr
    db = load_db()
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    return uid




@app.route('/')
def index():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    
    # If it's a new user with no cookie, generate a unique one
    is_new_user = False
    if not uid:
        import uuid
        uid = str(uuid.uuid4())[:8] # Short unique ID like 'a1b2c3d4'
        is_new_user = True
        
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    
    # Use make_response to attach the cookie
    resp = make_response(render_template('index.html', 
                           device_id=uid, 
                           points=db["users"][uid]["points"], 
                           logs=db["logs"][-5:]))
    
    # Set cookie to expire in 1 year (31,536,000 seconds)
    if is_new_user:
        resp.set_cookie('user_uuid', uid, max_age=31536000)
        
    return resp




@app.route('/api/status')
def get_status():
    uid = get_user_id()
    db = load_db()
    return jsonify({
        "state": session_data["state"],
        "session": session_data["count"],
        "points": db["users"][uid]["points"],
        "slots": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]],
        "is_my_session": session_data["active_user"] == uid
    })




@app.route('/api/start_session')
def web_start():
    uid = get_user_id()
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": uid, "last_activity": time.time()})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def web_stop():
    uid = get_user_id()
    if session_data["active_user"] == uid:
        db = load_db()
        added = session_data["count"]
        if added > 0:
            db["users"][uid]["points"] += added
            db["total_bottles"] += added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"])
            save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
        return jsonify({"status": "ok"})
    return jsonify({"status": "unauthorized"}), 401

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_user_id()
    db = load_db()
    if db["users"][uid]["points"] >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
        # 1 Pt = 5 Mins. Works even if running.
        start_or_extend_relay(slot, pts * 300)
        beep(2)
    return redirect(url_for('index'))

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")
    
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
