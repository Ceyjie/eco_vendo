import time, threading, os, subprocess, json, uuid
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- ORANGE PI ONE GPIO (Sysfs) ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "eco_database.json"
ADMIN_PASSWORD = "1234"

# --- DATABASE ENGINE ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

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

# --- ADMIN STATE ---
admin_data = {
    "active": False,
    "user_index": 0,
    "hold_start": 0,
    "holding": False
}
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
    global current_lcd_lines, lcd
    if not lcd:
        init_lcd()
        return
    try:
        safe_lines = []
        for line in new_lines:
            cleaned = ""
            for ch in str(line):
                if ch.isascii() and ch not in ('%', '\n', '\r', '\x00'):
                    cleaned += ch
                else:
                    cleaned += ' '
            safe_lines.append(cleaned.ljust(20)[:20])
        for i, line in enumerate(safe_lines):
            if line != current_lcd_lines[i]:
                lcd.cursor_pos = (i, 0)
                lcd.write_string(line)
                current_lcd_lines[i] = line
    except Exception as e:
        print(f"LCD error: {e}")
        lcd = None
        current_lcd_lines = ["", "", "", ""]
        init_lcd()

def format_time(seconds):
    seconds = max(0, seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}" if m < 60 else f"{m//60:02d}:{m%60:02d}"

# --- RELAY LOGIC ---
def start_or_extend_relay(slot, additional_seconds):
    is_already_running = slot_status[slot] > 0
    slot_status[slot] += additional_seconds
    if not is_already_running:
        threading.Thread(target=run_relay_thread, args=(slot,), daemon=True).start()

def run_relay_thread(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] = max(0, slot_status[slot] - 1)
    gpio_write(PINS_RELAYS[slot], 0)

# --- HARDWARE BUTTON HANDLERS ---
def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]

    # --- ADMIN MODE ACTIONS ---
    if admin_data["active"]:
        if pin == PIN_BTN_SELECT:
            # Scroll to next user
            db = load_db()
            users = list(db["users"].items())
            if users:
                admin_data["user_index"] = (admin_data["user_index"] + 1) % len(users)
            beep(1)
        elif pin == PIN_BTN_START:
            # Reset system
            system_refresh()
            beep(3)
        elif pin == PIN_BTN_CONFIRM:
            # Exit admin, return to IDLE
            admin_data["active"] = False
            session_data.update({"state": "IDLE", "count": 0, "active_user": None})
            beep(2)
        return  # Don't fall through to normal handlers

    # --- NORMAL MODE ACTIONS ---
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
    last_press = {p: 0.0 for p in btn_pins}  # FIXED: per-button debounce
    ir_cooldown = 0

    while True:
        now = time.time()

        # --- ADMIN ENTRY: hold START + SELECT together for 2 seconds ---
        start_held = gpio_read(PIN_BTN_START) == 0
        select_held = gpio_read(PIN_BTN_SELECT) == 0
        if start_held and select_held:
            if not admin_data["holding"]:
                admin_data["holding"] = True
                admin_data["hold_start"] = now
            elif (now - admin_data["hold_start"]) >= 2.0 and not admin_data["active"]:
                admin_data["active"] = True
                admin_data["user_index"] = 0
                admin_data["holding"] = False
                beep(3)
        else:
            admin_data["holding"] = False

        # --- BUTTONS: per-button debounce ---
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_val[p] == 1:
                if (now - last_press[p]) > 0.3:
                    last_press[p] = now
                    handle_physical_press(p)
            last_val[p] = val

        # --- IR SENSOR ---
        # Both HIGH (top + bottom) = large bottle = 2 points
        # Bottom HIGH, top LOW = small bottle = 1 point
        if session_data["state"] == "INSERTING" and now >= ir_cooldown and not admin_data["active"]:
            bot = gpio_read(PIN_IR_BOTTOM)
            top = gpio_read(PIN_IR_TOP)
            if bot == 1 and top == 1:
                session_data["count"] += 2
                session_data["last_activity"] = now
                beep(2)
                ir_cooldown = now + 0.6
            elif bot == 1 and top == 0:
                session_data["count"] += 1
                session_data["last_activity"] = now
                beep(1)
                ir_cooldown = now + 0.6

        # --- AUTO TIMEOUT: 60s inactivity (paused during admin) ---
        if not admin_data["active"]:
            if session_data["state"] not in ["IDLE", "THANK_YOU"]:
                if (now - session_data["last_activity"]) > 35:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
        else:
            # Keep last_activity fresh so session doesn't timeout while in admin
            session_data["last_activity"] = now

        time.sleep(0.01)

def display_manager():
    while True:
        s = session_data["state"]

        # --- ADMIN DISPLAY ---
        if admin_data["active"]:
            db = load_db()
            users = list(db["users"].items())
            total = db.get("total_bottles", 0)
            if users:
                idx = admin_data["user_index"] % len(users)
                uid, udata = users[idx]
                pts = udata.get("points", 0)
                lcd_write([
                    "  ** ADMIN MODE **  ",
                    f"ID:{uid[:12]}",
                    f"PTS:{pts}  {idx+1}/{len(users)}",
                    "B1:RST B2:NEXT B3:EXIT"
                ])
            else:
                lcd_write([
                    "  ** ADMIN MODE **  ",
                    f" TOTAL: {total} bottles",
                    "   No users yet",
                    "B1:RST        B3:EXIT"
                ])
            time.sleep(0.2)
            continue

        # --- NORMAL DISPLAY ---
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
    uid = request.cookies.get('user_uuid')
    if not uid:
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
    is_new_user = False
    if not uid:
        uid = str(uuid.uuid4())[:8]
        is_new_user = True
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    resp = make_response(render_template('index.html',
                           device_id=uid,
                           points=db["users"][uid]["points"],
                           logs=db["logs"][-5:]))
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

@app.route('/api/emergency_reset')
def admin_reset():
    system_refresh()
    return jsonify({"status": "system_refreshed"})

def system_refresh():
    for i in range(4): slot_status[i] = 0
    for pin in PINS_RELAYS: gpio_write(pin, 0)
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    beep(3)

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pass') != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    db = load_db()
    user_list = [{"user_id": k, "points": v.get("points", 0)} for k, v in db["users"].items()]
    return jsonify({"total_bottles": db.get("total_bottles", 0), "users": user_list})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_user_id()
    db = load_db()
    if db["users"][uid]["points"] >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
        start_or_extend_relay(slot, pts * 300)
        beep(2)
    return redirect(url_for('index'))

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
        gpio_setup(p, "in")
    for p in PINS_RELAYS:
        gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")

    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
