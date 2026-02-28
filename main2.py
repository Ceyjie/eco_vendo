import sqlite3, uuid, time, threading, os
from flask import Flask, render_template, request, redirect, jsonify, make_response
import OPi.GPIO as GPIO

# --- CONFIG ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")
PINS_RELAYS = [15, 16, 18, 22]
PIN_BUTTON = 11

active_slots = {}  # Memory Sync: {slot_index: end_timestamp}
session_data = {"active": False, "count": 0}

def run_timer(pin, sec, s_idx):
    GPIO.output(pin, GPIO.LOW) # Relay ON
    time.sleep(sec)
    GPIO.output(pin, GPIO.HIGH) # Relay OFF
    active_slots.pop(s_idx, None)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM active_timers WHERE slot=?', (s_idx,))
        conn.commit()

def init_hw():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    for p in PINS_RELAYS:
        GPIO.setup(p, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS user_balances (user_id TEXT PRIMARY KEY, points INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS active_timers (slot INTEGER PRIMARY KEY, end_time REAL)')
        conn.commit()

app = Flask(__name__)

@app.route('/')
def index():
    uid = request.cookies.get('device_id') or str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
    pts = res[0] if res else 0
    conn.close()
    resp = make_response(render_template('index.html', points=pts, device_id=uid))
    resp.set_cookie('device_id', uid, max_age=30*24*60*60, samesite='Lax')
    return resp

@app.route('/api/active_timers')
def get_timers():
    now = time.time()
    return jsonify({slot: {"name": "AC 220V" if slot == 3 else f"USB {slot+1}", "remaining": int(end - now)} 
                    for slot, end in active_slots.items() if end > now})

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pwd') != "eco123": return "Err", 401
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute('SELECT user_id, points FROM user_balances ORDER BY points DESC').fetchall()
    conn.close()
    return jsonify(users=[{"user_id": u[0], "points": u[1]} for u in users])

@app.route('/api/reset_all_timers')
def reset_all():
    if request.args.get('pwd') != "eco123": return "Err", 401
    for p in PINS_RELAYS: GPIO.output(p, GPIO.HIGH)
    active_slots.clear()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM active_timers')
        conn.commit()
    return jsonify(status="System Reset Successful")

@app.route('/api/start_session')
def start_s(): session_data.update({"active": True, "count": 0}); return "ok"

@app.route('/api/get_count')
def get_c(): return jsonify(count=session_data["count"])

@app.route('/api/stop_session')
def stop_s():
    uid, added = request.cookies.get('device_id'), session_data["count"]
    if added > 0:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT INTO user_balances VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET points=points+?', (uid, added, added))
        conn.commit(); conn.close()
    session_data["active"] = False
    return "ok"

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem_route(slot, pts):
    now = time.time()
    uid = request.cookies.get('device_id')
    conn = sqlite3.connect(DB_PATH)
    duration = pts * 300
    res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
    if res and res[0] >= pts:
        duration = pts * 300
        active_slots[slot] = now + duration
        conn.execute('UPDATE user_balances SET points = points - ? WHERE user_id=?', (pts, uid))
        conn.commit()
        threading.Thread(target=run_timer, args=(PINS_RELAYS[slot], duration, slot)).start()
    conn.close()
    return redirect('/')

def hw_loop():
    while True:
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            if session_data["active"]: session_data["count"] += 1
            time.sleep(0.4)
        time.sleep(0.1)

if __name__ == '__main__':
    init_hw(); init_db()
    threading.Thread(target=hw_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
