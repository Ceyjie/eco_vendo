import sqlite3, uuid, time, threading, os, subprocess
from flask import Flask, render_template, request, redirect, jsonify, make_response
import OPi.GPIO as GPIO
from RPLCD.i2c import CharLCD

# --- CONFIG ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")

# Hardware Pins (BOARD Numbering)
PINS_RELAYS = [15, 22, 24, 26]
PIN_IR_BOTTOM, PIN_IR_TOP = 7, 11
PIN_BUZZER = 13
PIN_BTN_START   = 8
PIN_BTN_SELECT  = 10
PIN_BTN_CONFIRM = 12

SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# --- STATE ---
session_data = {"active": False, "count": 0}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE", "selected_slot": 0, "points": 0, "uid": None}
ui_lock      = threading.Lock()

# --- LCD INITIALIZATION ---
try:
    # Adding charmap='A00' is critical for H3 chip stability
    lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4, charmap='A00')
except:
    try:
        lcd = CharLCD('PCF8574', 0x3f, port=0, cols=20, rows=4, charmap='A00')
    except Exception as e:
        print(f"LCD could not be initialized: {e}")
        lcd = None

def lcd_clear_write(lines):
    if not lcd: return
    try:
        lcd.clear()
        time.sleep(0.05) # Timing delay for Orange Pi One
        for i, line in enumerate(lines[:4]):
            lcd.cursor_pos = (i, 0)
            lcd.write_string(line[:20])
            time.sleep(0.01)
    except:
        pass

# --- HARDWARE HELPERS ---
def loud_beep(dur):
    try:
        GPIO.output(PIN_BUZZER, 1); time.sleep(dur); GPIO.output(PIN_BUZZER, 0)
    except:
        pass

def init_hw():
    print("[SYSTEM] Configuring Hardware...")
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    
    # Setup Inputs (Must be wired to GND)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Setup Outputs
    for p in PINS_RELAYS:
        GPIO.setup(p, GPIO.OUT, initial=GPIO.HIGH) # Relay OFF (Active Low)
    GPIO.setup(PIN_BUZZER, GPIO.OUT, initial=GPIO.LOW)

    lcd_clear_write([
        "  ECO-CHARGE VENDO", 
        "--------------------", 
        " Press START button", 
        "    to begin..."
    ])

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS user_balances (user_id TEXT PRIMARY KEY, points INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, points TEXT, timestamp TEXT)')
        conn.commit()

# --- LOGIC ---
def run_relay(slot, seconds):
    GPIO.output(PINS_RELAYS[slot], 0) # ON
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    GPIO.output(PINS_RELAYS[slot], 1) # OFF
    slot_status[slot] = 0

def on_btn_start():
    with ui_lock:
        if ui_state["state"] in ["IDLE", "DONE"]:
            ui_state["state"] = "INSERTING"
            session_data.update({"active": True, "count": 0})
            print("[BTN] Start Pressed - Beeping 3 times")
            # 3 Beeps for starting
            for _ in range(3):
                loud_beep(0.1)
                time.sleep(0.1)
            lcd_clear_write(["   INSERT BOTTLE", "", "   Count: 0", " [CONFIRM] to save"])

def on_btn_select():
    with ui_lock:
        if ui_state["state"] == "SELECTING":
            ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
            slot = ui_state["selected_slot"]
            busy = "BUSY" if slot_status[slot] > 0 else "Ready"
            loud_beep(0.05)
            lcd_clear_write([" SELECT OUTPUT:", f" > {SLOT_NAMES[slot]}", f" Status: {busy}", " [CONFIRM] to use"])

def on_btn_confirm():
    with ui_lock:
        state = ui_state["state"]
    if state == "INSERTING":
        count = session_data["count"]
        session_data["active"] = False
        uid = str(uuid.uuid4())[:8]
        with sqlite3.connect(DB_PATH) as conn:
            if count > 0:
                conn.execute('INSERT INTO user_balances VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET points=points+?', (uid, count, count))
                conn.execute('INSERT INTO logs (user_id,action,points,timestamp) VALUES(?,?,?,?)', (uid, "Deposit", f"+{count}", time.strftime("%H:%M")))
                conn.commit()
            res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
            pts = res[0] if res else 0
        with ui_lock:
            ui_state.update({"state": "SELECTING", "uid": uid, "points": pts, "selected_slot": 0})
        loud_beep(0.15)
        lcd_clear_write([f" Bottles: {count}", f" Pts: {pts}", f" > {SLOT_NAMES[0]}", " [SEL] to change"])
    elif state == "SELECTING":
        with ui_lock:
            slot, uid = ui_state["selected_slot"], ui_state["uid"]
        if slot_status[slot] > 0:
            lcd_clear_write(["   SLOT BUSY!", " Choose another", "", " [SEL] to cycle"])
            return
        cost = 1
        with sqlite3.connect(DB_PATH) as conn:
            res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
            if not res or res[0] < cost:
                lcd_clear_write([" NO POINTS LEFT", " Insert more!", "", " Press START"])
                with ui_lock: ui_state["state"] = "DONE"
                return
            conn.execute('UPDATE user_balances SET points=points-? WHERE user_id=?', (cost, uid))
            conn.execute('INSERT INTO logs (user_id,action,points,timestamp) VALUES(?,?,?,?)', (uid, SLOT_NAMES[slot], f"-{cost}", time.strftime("%H:%M")))
            conn.commit()
        threading.Thread(target=run_relay, args=(slot, cost * 300), daemon=True).start()
        loud_beep(0.3)
        with ui_lock: ui_state["state"] = "DONE"
        lcd_clear_write(["   ACTIVATED!", f" {SLOT_NAMES[slot]}", " Time: 5 min", " Press START"])

# --- LOOPS (Optimized to save RAM/CPU) ---
def button_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    while True:
        for pin, func in [(PIN_BTN_START, on_btn_start), (PIN_BTN_SELECT, on_btn_select), (PIN_BTN_CONFIRM, on_btn_confirm)]:
            try:
                curr = GPIO.input(pin)
                if curr == 0 and last[pin] == 1:
                    func()
                    time.sleep(0.3)
                last[pin] = curr
            except:
                pass
        time.sleep(0.1) # Prevents "Killed" by lowering CPU usage

def ir_loop():
    while True:
        try:
            if GPIO.input(PIN_IR_BOTTOM) == 0 and GPIO.input(PIN_IR_TOP) == 0:
                if session_data["active"]:
                    session_data["count"] += 1
                    loud_beep(0.1)
                    lcd_clear_write(["   INSERT BOTTLE", f"   Count: {session_data['count']}", "", " [CONFIRM] to save"])
                    time.sleep(0.8)
        except:
            pass
        time.sleep(0.1) # Prevents "Killed" by lowering CPU usage

# --- FLASK ---
app = Flask(__name__)
@app.route('/')
def index():
    uid = request.cookies.get('device_id') or str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
    pts = res[0] if res else 0
    logs = conn.execute('SELECT action, points, timestamp FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 5', (uid,)).fetchall()
    conn.close()
    resp = make_response(render_template('index.html', points=pts, logs=logs, device_id=uid))
    resp.set_cookie('device_id', uid, max_age=2592000)
    return resp

@app.route('/api/status')
def get_status():
    return jsonify({"session": session_data["count"], "slots": slot_status})

# --- BOOTSTRAP ---
if __name__ == '__main__':
    try:
        # Step 1: Force kill any background process on Port 5000
        print("[SYSTEM] Clearing Port 5000...")
        subprocess.run(["sudo", "fuser", "-k", "5000/tcp"], capture_output=True)
        time.sleep(1)

        # Step 2: Initialize
        init_hw()
        init_db()
        
        # Step 3: Start Threads
        threading.Thread(target=ir_loop, daemon=True).start()
        threading.Thread(target=button_loop, daemon=True).start()
        
        # Step 4: Run Flask (Lightweight settings)
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)

    except KeyboardInterrupt:
        print("\n[SYSTEM] Shutting down...")
    finally:
        try:
            lcd.clear()
            lcd.backlight_enabled = False
            for p in PINS_RELAYS: GPIO.output(p, GPIO.HIGH)
            GPIO.cleanup()
        except:
            pass
