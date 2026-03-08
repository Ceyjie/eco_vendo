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
ADMIN_PASSWORD = "1234"

# --- SYSTEM STATE ---
session_data = {
    "state": "IDLE", 
    "count": 0, 
    "active_user": None,
    "selected_slot": 0,
    "add_time_choice": 1, 
    "last_activity": time.time(),
    "sensor_lockout_until": 0
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- DATABASE ---
def load_db():
    if not os.path.exists(DB_FILE): return {"total_bottles": 0, "users": {}, "logs": []}
    try:
        with open(DB_FILE, 'r') as f: return json.load(f)
    except: return {"total_bottles": 0, "users": {}, "logs": []}

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

def get_user_id():
    uid = request.cookies.get('user_uuid')
    return uid if uid else request.remote_addr

# --- GPIO ---
def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    try:
        with open(f"{path}/direction", "w") as f: f.write(direction)
        if direction == "out":
            with open(f"{path}/value", "w") as f: f.write(value)
    except: pass

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

# --- LCD ---
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

# --- RELAYS ---
def start_or_extend_relay(slot, additional_seconds):
    is_running = slot_status[slot] > 0
    slot_status[slot] += additional_seconds
    if not is_running:
        threading.Thread(target=relay_worker, args=(slot,), daemon=True).start()

def relay_worker(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] -= 1
    gpio_write(PINS_RELAYS[slot], 0)

# --- HARDWARE LOOP ---
def hardware_loop():
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_btn_state = {p: 1 for p in btn_pins}
    while True:
        now = time.time()
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_btn_state[p] == 1:
                if (now - session_data["last_activity"]) > 0.15:
                    handle_physical_press(p)
            last_btn_state[p] = val

        if session_data["state"] == "INSERTING" and now > session_data["sensor_lockout_until"]:
            if gpio_read(PIN_IR_BOTTOM) == 0:
                points = 2 if gpio_read(PIN_IR_TOP) == 0 else 1
                session_data["count"] += points
                session_data["last_activity"] = now
                session_data["sensor_lockout_until"] = now + 1.0 
                beep(1)
        time.sleep(0.01)

def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]
    if pin == PIN_BTN_START and s == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL"})
        beep(1)
    elif pin == PIN_BTN_CONFIRM and s == "INSERTING":
        if session_data["count"] > 0: session_data["state"] = "SELECTING"
        else: session_data.update({"state": "IDLE", "active_user": None})
        beep(1)
    elif pin == PIN_BTN_SELECT and s == "SELECTING":
        session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        beep(1)
    elif pin == PIN_BTN_CONFIRM and s == "SELECTING":
        finalize_transaction()
        beep(1)

def finalize_transaction():
    slot = session_data["selected_slot"]
    pts = int(session_data["count"])
    db = load_db()
    uid = session_data["active_user"] if session_data["active_user"] != "LOCAL" else "LOCAL_USER"
    if uid not in db["users"]: db["users"][uid] = {"points": 0}
    db["users"][uid]["points"] += pts
    db["total_bottles"] += pts
    db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", SLOT_NAMES[slot]])
    save_db(db)
    start_or_extend_relay(slot, pts * 300)
    session_data["state"] = "THANK_YOU"
    beep(2)
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0, "active_user": None})).start()

# --- LCD MANAGER ---
def display_manager():
    while True:
        s = session_data["state"]
        if s == "IDLE":
            db = load_db()
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", f"   TOTAL: {db['total_bottles']} BTL", t1, t2])
        elif s == "INSERTING":
            lcd_write(["   INSERT BOTTLE", f"    POINTS: {int(session_data['count'])}", f"    TIME: {int(session_data['count']*5)}m", "B3: CONFIRM"])
        elif s == "SELECTING":
            lcd_write(["   SELECT SLOT", f"   > {SLOT_NAMES[session_data['selected_slot']]}", f"   FOR {int(session_data['count']*5)} MINS", "B2:NEXT B3:OK"])
        elif s == "THANK_YOU":
            lcd_write(["      THANK YOU!      ", " You helped protect ", " our environment by ", " recycling plastic! "])
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

@app.route('/api/status')
def get_status():
    db = load_db(); uid = get_user_id()
    return jsonify({
        "state": session_data["state"], "session": int(session_data["count"]),
        "points": db["users"].get(uid, {"points": 0})["points"],
        "slots": [slot_status[i] for i in range(4)],
        "is_my_session": session_data["active_user"] == uid
    })

@app.route('/api/emergency_reset')
def admin_reset():
    for i in range(4):
        slot_status[i] = 0
        gpio_write(PINS_RELAYS[i], "0")
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    beep(3)
    lcd_write(["   SYSTEM RESET", "   ALL SLOTS OFF", "   RETURNING IDLE", "--------------------"])
    return jsonify({"status": "system_refreshed"})

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pass') != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    db = load_db()
    user_list = [{"user_id": k, "points": v.get("points", 0)} for k, v in db["users"].items()]
    return jsonify({"total_bottles": db.get("total_bottles", 0), "users": user_list})

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
        if session_data["count"] > 0: finalize_transaction()
        else: session_data.update({"state": "IDLE", "count": 0, "active_user": None})
        return jsonify({"status": "ok"})
    return jsonify({"status": "unauthorized"}), 401

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_user_id(); db = load_db()
    if db["users"].get(uid, {}).get("points", 0) >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
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
    app.run(host='0.0.0.0', port=80, debug=False)
