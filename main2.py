import sqlite3, uuid, time, threading, os
from flask import Flask, render_template, request, redirect, jsonify, make_response
import OPi.GPIO as GPIO

# --- CONFIG ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")
PINS_RELAYS = [15, 16, 18, 22]
PIN_IR_BOTTOM, PIN_IR_TOP = 7, 11
PIN_BUZZER = 13

session_data = {"active": False, "count": 0}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

def loud_beep(dur):
    GPIO.output(PIN_BUZZER, 1); time.sleep(dur); GPIO.output(PIN_BUZZER, 0)

def init_hw():
    GPIO.setwarnings(False); GPIO.setmode(GPIO.BOARD)
    for p in PINS_RELAYS: GPIO.setup(p, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(PIN_BUZZER, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(PIN_IR_BOTTOM, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_IR_TOP, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS user_balances (user_id TEXT PRIMARY KEY, points INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, points TEXT, timestamp TEXT)')
        conn.commit()

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

@app.route('/api/start_session')
def start_s():
    session_data.update({"active": True, "count": 0})
    return "ok"

@app.route('/api/stop_session')
def stop_s():
    uid = request.cookies.get('device_id')
    if session_data["count"] > 0:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('INSERT INTO user_balances VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET points=points+?', (uid, session_data["count"], session_data["count"]))
            conn.execute('INSERT INTO logs (user_id, action, points, timestamp) VALUES (?, "Deposit", ?, ?)', (uid, f"+{session_data['count']}", time.strftime("%H:%M")))
            conn.commit()
    session_data["active"] = False
    return "ok"

def run_relay(slot, seconds):
    GPIO.output(PINS_RELAYS[slot], 0) # ON
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    GPIO.output(PINS_RELAYS[slot], 1) # OFF
    slot_status[slot] = 0

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_route(slot, pts):
    uid = request.cookies.get('device_id')
    with sqlite3.connect(DB_PATH) as conn:
        res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
        if res and res[0] >= pts and slot_status[slot] == 0:
            conn.execute('UPDATE user_balances SET points = points - ? WHERE user_id=?', (pts, uid))
            conn.execute('INSERT INTO logs (user_id, action, points, timestamp) VALUES (?, ?, ?, ?)', (uid, f"Slot {slot+1}", f"-{pts}", time.strftime("%H:%M")))
            conn.commit()
            threading.Thread(target=run_relay, args=(slot, pts * 300)).start()
    return redirect('/')

@app.route('/api/admin_stats')
def admin_stats():
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute('SELECT user_id, points FROM user_balances ORDER BY points DESC').fetchall()
    total = conn.execute('SELECT SUM(CAST(points AS INTEGER)) FROM logs WHERE action="Deposit"').fetchone()[0] or 0
    conn.close()
    return jsonify(users=[{"user_id": u[0], "points": u[1]} for u in users], total_bottles=total)

@app.route('/api/admin_update_points')
def admin_update_points():
    uid = request.args.get('uid'); action = request.args.get('action')
    val = 1 if action == 'add' else -1
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('UPDATE user_balances SET points = MAX(0, points + ?) WHERE user_id = ?', (val, uid))
        conn.commit()
    return "ok"

@app.route('/api/admin_reset')
def admin_reset():
    for i in range(4):
        slot_status[i] = 0
        GPIO.output(PINS_RELAYS[i], 1)
    return "ok"

def hw_loop():
    while True:
        if GPIO.input(PIN_IR_BOTTOM) == 0 and GPIO.input(PIN_IR_TOP) == 0:
            if session_data["active"]:
                session_data["count"] += 1
                threading.Thread(target=loud_beep, args=(0.15,)).start()
                time.sleep(0.6)
        time.sleep(0.1)

if __name__ == '__main__':
    init_hw(); init_db()
    threading.Thread(target=hw_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000) # Changed from 80 to 5000
