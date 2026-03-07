import time, threading, os, subprocess, sqlite3, uuid
from flask import Flask, render_template, request, jsonify, redirect, session
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"
PIN_BTN_SELECT  = "14"
PIN_BTN_CONFIRM = "110"

# Sysfs IDs for Relays (Active High Logic: 1=ON, 0=OFF)
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"active": False, "count": 0, "current_user": None}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE", "selected_slot": 0}

# --- DATABASE LOGIC ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (total_bottles INTEGER)''')
    c.execute("SELECT * FROM stats")
    if not c.fetchone(): c.execute("INSERT INTO stats VALUES (0)")
    conn.commit()
    conn.close()

def update_user_points(uid, pts):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (pts, uid))
    if pts > 0:
        c.execute("UPDATE stats SET total_bottles = total_bottles + ?", (pts,))
    conn.commit()
    conn.close()

def get_user_points(uid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE user_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

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

# --- RELAY LOGIC (ACTIVE HIGH) ---
def run_relay_timer(slot, seconds):
    # ACTIVE HIGH: 1 to turn ON
    gpio_write(PINS_RELAYS[slot], 1)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    # ACTIVE HIGH: 0 to turn OFF
    gpio_write(PINS_RELAYS[slot], 0)
    slot_status[slot] = 0

# --- SESSION LOGIC ---
def on_btn_start(uid="LOCAL"):
    if ui_state["state"] in ["IDLE", "DONE"]:
        ui_state["state"] = "INSERTING"
        session_data.update({"active": True, "count": 0, "current_user": uid})
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
        lcd_write(["   INSERT BOTTLE", f" ID: {uid}", " BOTTLES: 0", " [DONE] via App"])

def on_btn_confirm():
    if ui_state["state"] == "INSERTING":
        uid = session_data["current_user"]
        if session_data["count"] > 0:
            update_user_points(uid, session_data["count"])
        ui_state["state"] = "IDLE"
        session_data["active"] = False
        lcd_write([" BOTTLES SAVED!", " Points Updated", " Returning...", ""])
        time.sleep(2)

# --- HARDWARE LOOPS ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start("LOCAL")
                elif p == PIN_BTN_CONFIRM: on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        if session_data["active"]:
            # IR Sensors logic (Both triggered = bottle detected)
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                lcd_write(["   INSERT BOTTLE", f" ID: {session_data['current_user']}", f" BOTTLES: {session_data['count']}", " [DONE] via App"])
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "    READY TO SCAN", t1, t2])
        time.sleep(1)

# --- FLASK WEB SERVER ---
app = Flask(__name__)
app.secret_key = "eco_music_99"

@app.route('/')
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())[:6].upper()
    uid = session['user_id']
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (uid,))
    conn.commit()
    conn.close()
    return render_template('index.html', device_id=uid, points=get_user_points(uid), logs=[])

@app.route('/api/status')
def get_status():
    return jsonify({
        "session": session_data["count"],
        "points": get_user_points(session.get('user_id', '')),
        "slots": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]],
        "state": ui_state["state"]
    })

@app.route('/api/start_session')
def start_api():
    on_btn_start(session.get('user_id', 'GUEST'))
    return jsonify({"status": "started"})

@app.route('/api/stop_session')
def stop_api():
    on_btn_confirm()
    return jsonify({"status": "stopped"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_web(slot, pts):
    uid = session.get('user_id')
    if get_user_points(uid) < pts: return "Error: Not enough points", 400
    if slot_status[slot] > 0: return "Error: Slot Busy", 400
    
    update_user_points(uid, -pts)
    # 300 seconds (5 mins) per point
    threading.Thread(target=run_relay_timer, args=(slot, pts * 300), daemon=True).start()
    return redirect('/')

@app.route('/api/admin_stats')
def admin_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT total_bottles FROM stats")
    total = c.fetchone()[0]
    c.execute("SELECT user_id, points FROM users")
    users = [{"user_id": r[0], "points": r[1]} for r in c.fetchall()]
    conn.close()
    return jsonify({"total_bottles": total, "users": users})

# --- MAIN ENTRY ---
if __name__ == '__main__':
    init_db()
    # Kill existing flask instances on port 80
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    
    # Setup Inputs
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: 
        gpio_setup(p, "in")
    
    # Setup Relay Outputs (Active High: Start at 0)
    for p in PINS_RELAYS: 
        gpio_setup(p, "out", "0")
        
    # Setup Buzzer
    gpio_setup(PIN_BUZZER, "out", "0")
    
    # Start Background Threads
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    
    # Run Web Server
    app.run(host='0.0.0.0', port=80)
