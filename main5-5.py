import time, threading, os, subprocess, sqlite3, uuid
from flask import Flask, jsonify, render_template, request, session, redirect

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"state": "IDLE", "count": 0, "slots": [0, 0, 0, 0], "last_activity": time.time()}

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    conn.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, action TEXT, date TEXT)')
    conn.commit()
    conn.close()

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
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

def beep(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0); time.sleep(0.05)

# --- RELAY TIMER ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 0) # ON
    while seconds > 0:
        session_data["slots"][slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 1) # OFF
    session_data["slots"][slot] = 0

# --- FLASK APP ---
app = Flask(__name__)
app.secret_key = "eco_vendo_key"

@app.route('/')
def index():
    if 'uid' not in session:
        session['uid'] = str(uuid.uuid4())[:8].upper()
    
    uid = session['uid']
    conn = sqlite3.connect(DB_FILE)
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    if not user:
        conn.execute('INSERT INTO users VALUES (?, 0)', (uid,))
        conn.commit()
        pts = 0
    else: pts = user[0]
    
    logs = conn.execute('SELECT action, date FROM logs WHERE user_id = ? ORDER BY date DESC LIMIT 5', (uid,)).fetchall()
    conn.close()
    return render_template('index.html', points=pts, device_id=uid, logs=[(l[0], "", l[1]) for l in logs])

@app.route('/api/status')
def get_status():
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute('SELECT points FROM users WHERE user_id = ?', (session.get('uid',''),)).fetchone()
    conn.close()
    return jsonify({
        "points": res[0] if res else 0,
        "session": session_data["count"],
        "slots": session_data["slots"]
    })

@app.route('/api/start_session')
def start_session():
    session_data["state"] = "INSERTING"
    session_data["count"] = 0
    beep(1)
    return jsonify({"status": "ok"})

@app.route('/api/stop_session')
def stop_session():
    if session_data["count"] > 0:
        uid = session['uid']
        conn = sqlite3.connect(DB_FILE)
        conn.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (session_data["count"], uid))
        conn.execute('INSERT INTO logs VALUES (?, ?, ?)', (uid, f"Saved {session_data['count']} Bottles", time.strftime("%H:%M")))
        conn.commit()
        conn.close()
    session_data["state"] = "IDLE"
    session_data["count"] = 0
    beep(2)
    return jsonify({"status": "ok"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = session['uid']
    conn = sqlite3.connect(DB_FILE)
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    if user and user[0] >= pts:
        conn.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (pts, uid))
        conn.execute('INSERT INTO logs VALUES (?, ?, ?)', (uid, f"Used {pts} Pts", time.strftime("%H:%M")))
        conn.commit()
        threading.Thread(target=run_relay_timer, args=(slot, pts * 300), daemon=True).start()
        beep(1)
    conn.close()
    return redirect('/')

@app.route('/api/admin_stats')
def admin_stats():
    conn = sqlite3.connect(DB_FILE)
    total = conn.execute('SELECT SUM(points) FROM users').fetchone()[0] or 0
    users = conn.execute('SELECT user_id, points FROM users ORDER BY points DESC').fetchall()
    conn.close()
    return jsonify({"total_bottles": total, "users": [{"user_id": u[0], "points": u[1]} for u in users]})

@app.route('/api/admin_update_points')
def admin_update():
    uid, action = request.args.get('uid'), request.args.get('action')
    val = 1 if action == 'add' else -1
    conn = sqlite3.connect(DB_FILE)
    conn.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (val, uid))
    conn.commit()
    conn.close()
    return "ok"

# --- HARDWARE LOOP ---
def hardware_loop():
    while True:
        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                beep(1)
                time.sleep(0.7)
        time.sleep(0.1)

if __name__ == '__main__':
    init_db()
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "1")
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
