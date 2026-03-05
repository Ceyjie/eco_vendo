import time, threading, os, subprocess, signal, sys, sqlite3, uuid
from flask import Flask, render_template, request, jsonify, redirect, session
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"   # Button 1
PIN_BTN_SELECT  = "14"   # Button 2
PIN_BTN_CONFIRM = "110"  # Button 3
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"active": False, "count": 0, "current_user": "LOCAL"}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE", "selected_slot": 0}

# --- DATABASE LOGIC (Kept as requested) ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (total_bottles INTEGER)''')
    c.execute("SELECT * FROM stats")
    if not c.fetchone(): c.execute("INSERT INTO stats VALUES (0)")
    conn.commit()
    conn.close()

def update_user_points(uid, pts):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (uid,))
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
    gpio_write(PINS_RELAYS[slot], 0) # Relay ON (Active Low)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 1) # Relay OFF
    slot_status[slot] = 0

def on_btn_start():
    ui_state["state"] = "INSERTING"
    session_data.update({"active": True, "count": 0})
    gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
    lcd_write(["   INSERT BOTTLE", "", " BOTTLES: 0", " [BTN 3] TO DONE"])

def on_btn_select():
    # Increment slot and skip busy ones
    attempts = 0
    while attempts < 4:
        ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
        if slot_status[ui_state["selected_slot"]] == 0:
            break # Found an available slot
        attempts += 1
    gpio_write(PIN_BUZZER, 1); time.sleep(0.05); gpio_write(PIN_BUZZER, 0)

def on_btn_confirm():
    if ui_state["state"] == "INSERTING":
        # Save points to the local machine session or active web user
        uid = session_data["current_user"]
        update_user_points(uid, session_data["count"])
        ui_state["state"] = "IDLE"
        session_data["active"] = False
        lcd_write(["   POINTS SAVED!", f" TOTAL: {session_data['count']}", "  READY TO USE", ""])
        time.sleep(2)

# --- LOOPS ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        # Physical Buttons logic
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: on_btn_start()
                elif p == PIN_BTN_SELECT: on_btn_select()
                elif p == PIN_BTN_CONFIRM: on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        # IR Sensor logic
        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                lcd_write(["   INSERT BOTTLE", "", f" BOTTLES: {session_data['count']}", " [BTN 3] TO DONE"])
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            # Display slots and highlight selected one with '>'
            lines = ["      ECO VENDO", "   CHOOSE & REDEEM"]
            sel = ui_state["selected_slot"]
            
            t1 = f"{'>' if sel==0 else ' '}U1:{format_time(slot_status[0])}  {'>' if sel==1 else ' '}U2:{format_time(slot_status[1])}"
            t2 = f"{'>' if sel==2 else ' '}U3:{format_time(slot_status[2])}  {'>' if sel==3 else ' '}AC:{format_time(slot_status[3])}"
            lines.append(t1)
            lines.append(t2)
            lcd_write(lines)
        time.sleep(0.5)

# --- FLASK ---
app = Flask(__name__)
app.secret_key = "eco_music_99"

@app.route('/')
def index():
    if 'user_id' not in session: session['user_id'] = str(uuid.uuid4())[:6].upper()
    uid = session['user_id']
    init_db()
    return render_template('index.html', device_id=uid, points=get_user_points(uid))

@app.route('/api/status')
def get_status():
    return jsonify({
        "session": session_data["count"],
        "points": get_user_points(session.get('user_id', 'LOCAL')),
        "slots": [slot_status[0], slot_status[1], slot_status[2], slot_status[3]],
        "state": ui_state["state"],
        "selected": ui_state["selected_slot"]
    })

@app.route('/api/start_session')
def start_api():
    on_btn_start()
    return jsonify({"status": "started"})

@app.route('/api/stop_session')
def stop_api():
    on_btn_confirm()
    return jsonify({"status": "stopped"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_web(slot, pts):
    uid = session.get('user_id', 'LOCAL')
    if get_user_points(uid) < pts: return "Error", 400
    if slot_status[slot] > 0: return "Busy", 400
    update_user_points(uid, -pts)
    threading.Thread(target=run_relay_timer, args=(slot, pts * 300), daemon=True).start()
    return redirect('/')

if __name__ == '__main__':
    init_db()
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
