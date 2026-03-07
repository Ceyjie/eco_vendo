import time, threading, os, json, uuid
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- CONFIGURATION ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "eco_database.json"
ADMIN_PASSWORD = "1234"

# --- DATABASE ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    try:
        with open(DB_FILE, 'r') as f: return json.load(f)
    except: return {"total_bottles": 0, "users": {}, "logs": []}

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE", "count": 0, "active_user": None,
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

# --- FIXED EMERGENCY RESET ---
def system_refresh():
    global slot_status
    # 1. First, zero out all timers. This signals relay_workers to stop.
    for i in range(4):
        slot_status[i] = 0
    
    # 2. Hard-kill the physical pins immediately
    for pin in PINS_RELAYS:
        gpio_write(pin, 0)
    
    # 3. Reset logic state
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    beep(3)

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
        # Check if status was reset to 0 by Admin during sleep
        if slot_status[slot] > 0:
            slot_status[slot] -= 1
        else:
            break # Exit immediately if reset
    gpio_write(PINS_RELAYS[slot], 0)

# --- HARDWARE LOOPS ---
def hardware_loop():
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}
    while True:
        now = time.time()
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_val[p] == 1:
                if (now - session_data["last_activity"]) > 0.2:
                    handle_physical_press(p)
            last_val[p] = val

        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                session_data["last_activity"] = now
                beep(1)
                time.sleep(0.6)
        time.sleep(0.01)

def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    
    # 1. Start Button: Trigger session if IDLE
    if pin == PIN_BTN_START and session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL"})
        beep(1)

    # 2. Confirm Button: Save bottles and go back to IDLE
    elif pin == PIN_BTN_CONFIRM and session_data["state"] == "INSERTING":
        db = load_db()
        added = session_data["count"]
        if added > 0:
            user_id = "LOCAL_USER"
            if user_id not in db["users"]: db["users"][user_id] = {"points": 0}
            db["users"][user_id]["points"] += added
            db["total_bottles"] += added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Physical"])
            save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
        beep(2)

def display_manager():
    while True:
        s = session_data["state"]
        if s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "      PRESS START", t1, t2])
        elif s == "INSERTING":
            # Show live update on LCD
            lcd_write(["   INSERT BOTTLE", f"    BOTTLES: {session_data['count']}", f"    TIME: {session_data['count']*5}m", "B3:CONFIRM"])
        time.sleep(0.1) # Faster update for responsiveness

# --- FLASK ROUTES ---
app = Flask(__name__)

@app.route('/')
def index():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    if not uid: uid = str(uuid.uuid4())[:8]
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    resp = make_response(render_template('index.html', device_id=uid, points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

@app.route('/api/status')
def get_status():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    user_pts = db["users"].get(uid, {"points": 0})["points"]
    return jsonify({
        "state": session_data["state"],
        "session": session_data["count"],
        "points": user_pts,
        "slots": [slot_status[i] for i in range(4)],
        "is_my_session": session_data["active_user"] == uid
    })

@app.route('/api/start_session')
def web_start():
    uid = request.cookies.get('user_uuid')
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": uid, "last_activity": time.time()})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def web_stop():
    uid = request.cookies.get('user_uuid')
    if session_data["active_user"] == uid:
        db = load_db()
        added = session_data["count"]
        if added > 0:
            db["users"][uid]["points"] += added
            db["total_bottles"] = db.get("total_bottles", 0) + added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"])
            save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    return jsonify({"status": "ok"})

@app.route('/api/emergency_reset')
def admin_reset():
    system_refresh()
    return jsonify({"status": "system_refreshed"})

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pass') != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    db = load_db()
    user_list = [{"user_id": k, "points": v.get("points", 0)} for k, v in db["users"].items()]
    return jsonify({"total_bottles": db.get("total_bottles", 0), "users": user_list})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = request.cookies.get('user_uuid')
    db = load_db()
    if db["users"].get(uid, {}).get("points", 0) >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
        start_or_extend_relay(slot, pts * 300) # 5 mins per point
        beep(2)
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: 
        gpio_setup(p, "in")
    for p in PINS_RELAYS: 
        gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")
    
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80, debug=False)
