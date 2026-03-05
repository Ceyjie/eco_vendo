import time, threading, os, subprocess, sqlite3, uuid
from flask import Flask, jsonify, render_template, request, session, redirect

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PINS_RELAYS = ["3", "2", "67", "21"]
DB_FILE = "vendo.db"

# --- STATE ---
session_data = {"state": "IDLE", "count": 0, "slots": [0, 0, 0, 0]}

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    # user_id is the unique device ID for the phone
    conn.execute('CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    # logs table for the history view
    conn.execute('CREATE TABLE IF NOT EXISTS logs (user_id TEXT, action TEXT, date TEXT)')
    conn.commit()
    conn.close()

def add_log(uid, msg):
    conn = sqlite3.connect(DB_FILE)
    now = time.strftime("%H:%M:%S")
    conn.execute('INSERT INTO logs VALUES (?, ?, ?)', (uid, msg, now))
    conn.commit()
    conn.close()

# --- HARDWARE HELPERS ---
def beep(times=1):
    for _ in range(times):
        # Using shell logic to trigger buzzer if libraries aren't loaded
        os.system(f"echo 1 > /sys/class/gpio/gpio{PIN_BUZZER}/value")
        time.sleep(0.1)
        os.system(f"echo 0 > /sys/class/gpio/gpio{PIN_BUZZER}/value")
        time.sleep(0.05)

def run_timer(slot_idx, seconds):
    os.system(f"echo 0 > /sys/class/gpio/gpio{PINS_RELAYS[slot_idx]}/value") # ON
    while seconds > 0:
        session_data["slots"][slot_idx] = seconds
        time.sleep(1)
        seconds -= 1
    os.system(f"echo 1 > /sys/class/gpio/gpio{PINS_RELAYS[slot_idx]}/value") # OFF
    session_data["slots"][slot_idx] = 0

# --- FLASK APP ---
app = Flask(__name__)
app.secret_key = "eco_secret_99"

@app.route('/')
def index():
    if 'uid' not in session:
        session['uid'] = str(uuid.uuid4())[:8].upper()
    
    conn = sqlite3.connect(DB_FILE)
    # Get user points
    row = conn.execute('SELECT points FROM users WHERE user_id = ?', (session['uid'],)).fetchone()
    if not row:
        conn.execute('INSERT INTO users VALUES (?, 0)', (session['uid'],))
        conn.commit()
        pts = 0
    else:
        pts = row[0]
        
    # Get recent logs
    logs = conn.execute('SELECT action, date FROM logs WHERE user_id = ? ORDER BY date DESC LIMIT 5', (session['uid'],)).fetchall()
    conn.close()
    
    # We pass action as the second element to match your HTML: log[0]=msg, log[1]=pts, log[2]=time
    # Here we simplify it to: msg, time
    return render_template('index.html', points=pts, device_id=session['uid'], logs=[(l[0], "", l[1]) for l in logs])

@app.route('/api/status')
def get_status():
    conn = sqlite3.connect(DB_FILE)
    pts = conn.execute('SELECT points FROM users WHERE user_id = ?', (session.get('uid',''),)).fetchone()
    conn.close()
    return jsonify({
        "points": pts[0] if pts else 0,
        "session": session_data["count"],
        "slots": session_data["slots"]
    })

@app.route('/api/start_session')
def start_session():
    session_data["count"] = 0
    session_data["state"] = "INSERTING"
    beep(1)
    return "OK"

@app.route('/api/stop_session')
def stop_session():
    if session_data["count"] > 0:
        uid = session['uid']
        conn = sqlite3.connect(DB_FILE)
        conn.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (session_data["count"], uid))
        conn.commit()
        conn.close()
        add_log(uid, f"Inserted {session_data['count']} Bottles")
    session_data["state"] = "IDLE"
    session_data["count"] = 0
    beep(2)
    return "OK"

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = session['uid']
    conn = sqlite3.connect(DB_FILE)
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    if user and user[0] >= pts:
        conn.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (pts, uid))
        conn.commit()
        conn.close()
        add_log(uid, f"Redeemed {pts} Pts (Slot {slot+1})")
        threading.Thread(target=run_timer, args=(slot, pts * 300), daemon=True).start()
        beep(1)
    return redirect('/')

# --- ADMIN ROUTES ---
@app.route('/api/admin_stats')
def admin_stats():
    conn = sqlite3.connect(DB_FILE)
    total = conn.execute('SELECT SUM(points) FROM users').fetchone()[0] or 0
    users = conn.execute('SELECT user_id, points FROM users ORDER BY points DESC').fetchall()
    conn.close()
    return jsonify({
        "total_bottles": total,
        "users": [{"user_id": u[0], "points": u[1]} for u in users]
    })

@app.route('/api/admin_update_points')
def admin_update():
    uid = request.args.get('uid')
    action = request.args.get('action')
    val = 1 if action == 'add' else -1
    conn = sqlite3.connect(DB_FILE)
    conn.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (val, uid))
    conn.commit()
    conn.close()
    return "OK"

# --- HARDWARE SENSOR LOOP ---
def sensor_loop():
    while True:
        if session_data["state"] == "INSERTING":
            # Read IR Sensors (Assuming Active Low)
            # You'll need to use your gpio_read function here
            # if gpio_read(PIN_IR_BOTTOM) == 0: ...
            pass
        time.sleep(0.1)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=sensor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
