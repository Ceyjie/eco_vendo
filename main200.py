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

PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"active": False, "count": 0, "current_user": "LOCAL"}
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
    gpio_write(PINS_RELAYS[slot], 1) # ON
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 0) # OFF
    slot_status[slot] = 0

# --- HYBRID LOGIC ---
def on_btn_start(uid="LOCAL"):
    if ui_state["state"] == "IDLE":
        ui_state["state"] = "INSERTING"
        session_data.update({"active": True, "count": 0, "current_user": uid})
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)

def on_btn_confirm():
    # If inserting bottles, save them
    if ui_state["state"] == "INSERTING":
        uid = session_data["current_user"]
        if session_data["count"] > 0:
            update_user_points(uid, session_data["count"])
        ui_state["state"] = "IDLE"
        session_data["active"] = False
        lcd_write(["   POINTS SAVED!", f" User: {uid}", " Returning...", ""])
        time.sleep(2)
    
    # If in Idle/Redeem mode, activate the selected slot
    elif ui_state["state"] == "IDLE":
        uid = "LOCAL" # Assuming local physical buttons use a generic local wallet
        if get_user_points(uid) >= 1:
            slot = ui_state["selected_slot"]
            if slot_status[slot] == 0:
                update_user_points(uid, -1)
                threading.Thread(target=run_relay_timer, args=(slot, 300), daemon=True).start()
                gpio_write(PIN_BUZZER, 1); time.sleep(0.2); gpio_write(PIN_BUZZER, 0)

# --- HARDWARE LOOP ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                if p == PIN_BTN_START: 
                    on_btn_start("LOCAL")
                elif p == PIN_BTN_SELECT:
                    # Cycle through 4 slots
                    ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
                elif p == PIN_BTN_CONFIRM: 
                    on_btn_confirm()
                time.sleep(0.2)
            last[p] = val

        # Sensor logic
        if session_data["active"]:
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            sel = ui_state["selected_slot"]
            # Visual indicator for which slot is selected on the physical machine
            lines = [
                "    --- IDLE ---",
                f"Points: {get_user_points('LOCAL')}",
                f"{'>' if sel==0 else ' '}U1:{format_time(slot_status[0])} {'>' if sel==1 else ' '}U2:{format_time(slot_status[1])}",
                f"{'>' if sel==2 else ' '}U3:{format_time(slot_status[2])} {'>' if sel==3 else ' '}AC:{format_time(slot_status[3])}"
            ]
            lcd_write(lines)
        elif ui_state["state"] == "INSERTING":
            lcd_write([
                "  INSERT BOTTLES",
                f"User: {session_data['current_user']}",
                f"Count: {session_data['count']}",
                "Press [OK] to save"
            ])
        time.sleep(0.5)

# --- FLASK ---
app = Flask(__name__)
app.secret_key = "eco_music_99"

@app.route('/')
def index():
    if 'user_id' not in session: session['user_id'] = str(uuid.uuid4())[:6].upper()
    uid = session['user_id']
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (uid,))
    conn.commit(); conn.close()
    return render_template('index.html', device_id=uid, points=get_user_points(uid))

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_web(slot, pts):
    uid = session.get('user_id')
    if get_user_points(uid) >= pts and slot_status[slot] == 0:
        update_user_points(uid, -pts)
        threading.Thread(target=run_relay_timer, args=(slot, pts * 300), daemon=True).start()
    return redirect('/')

# (Rest of API routes remain same as previous version)
@app.route('/api/status')
def get_status():
    return jsonify({"session": session_data["count"], "slots": [slot_status[i] for i in range(4)], "state": ui_state["state"]})

if __name__ == '__main__':
    init_db()
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0") # ACTIVE HIGH START OFF
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
