import time, threading, os, subprocess, json, uuid
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- ORANGE PI ONE GPIO (Sysfs Numbers) ---
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
    "add_time_choice": 1, 
    "last_activity": time.time()
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- DATABASE ENGINE ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# --- GPIO LOW LEVEL ---
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

# --- LCD ENGINE (Anti-Blink) ---
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

# --- SYSTEM REFRESH / CANCEL ---
def system_refresh():
    global slot_status
    for p in PINS_RELAYS: gpio_write(p, 0)
    for i in range(4): slot_status[i] = 0
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    beep(3)

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
        slot_status[slot] -= 1
    gpio_write(PINS_RELAYS[slot], 0)

# --- HARDWARE LOOP ---
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
        time.sleep(0.01)

def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]
    if pin == PIN_BTN_START and s == "IDLE":
        beep(1); session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL"})
    elif pin == PIN_BTN_SELECT:
        if s == "SELECTING":
            beep(1); session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        elif s == "ADD_TIME_PROMPT":
            beep(1); session_data["add_time_choice"] = 1 - session_data["add_time_choice"]
    elif pin == PIN_BTN_CONFIRM:
        if s == "INSERTING":
            beep(1); session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        elif s == "SELECTING":
            if slot_status[session_data["selected_slot"]] > 0:
                beep(1); session_data["state"] = "ADD_TIME_PROMPT"
            else: finalize_txn()
        elif s == "ADD_TIME_PROMPT":
            if session_data["add_time_choice"] == 1: finalize_txn()
            else: session_data["state"] = "SELECTING"

def finalize_txn():
    slot = session_data["selected_slot"]
    start_or_extend_relay(slot, session_data["count"] * 300)
    session_data["state"] = "THANK_YOU"
    lcd_write(["     THANK YOU!", "  BOTTLES RECYCLED", f"   {session_data['count']} BOTTLES", "   SYSTEM SAVED"])
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0, "active_user": None})).start()

# --- WEB LOGIC ---
app = Flask(__name__)

def get_uid():
    uid = request.cookies.get('user_uuid')
    return uid if uid else request.remote_addr

@app.route('/')
def index():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    resp_needed = False
    if not uid:
        uid = str(uuid.uuid4())[:8]
        resp_needed = True
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}; save_db(db)
    
    resp = make_response(render_template('index.html', device_id=uid, points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    if resp_needed: resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

@app.route('/api/status')
def get_status():
    uid = get_uid()
    db = load_db()
    return jsonify({
        "state": session_data["state"], "session": session_data["count"],
        "points": db["users"].get(uid, {}).get("points", 0),
        "slots": [slot_status[i] for i in range(4)],
        "is_my_session": session_data["active_user"] == uid
    })

@app.route('/api/start_session')
def web_start():
    uid = get_uid()
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": uid, "last_activity": time.time()})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def web_stop():
    uid = get_uid()
    if session_data["active_user"] == uid:
        db = load_db(); added = session_data["count"]
        if added > 0:
            db["users"][uid]["points"] += added
            db["total_bottles"] += added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"])
            save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    return jsonify({"status": "ok"})

@app.route('/api/emergency_reset')
def emergency_reset():
    system_refresh(); return jsonify({"status": "refreshed"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_uid(); db = load_db()
    if db["users"][uid]["points"] >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
        start_or_extend_relay(slot, pts * 300); beep(2)
    return redirect(url_for('index'))

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(1), [ (display_manager(), time.sleep(0.1)) for _ in iter(int, 1) ]), daemon=True).start()
    app.run(host='0.0.0.0', port=80)
