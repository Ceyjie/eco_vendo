import sqlite3
import uuid
import time
import threading
import os
import subprocess
import signal
import atexit
import sys
import secrets
from flask import Flask, render_template, request, redirect, jsonify, make_response
import OPi.GPIO as GPIO 

# --- SETTINGS ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")

# --- GPIO DEFINITIONS ---
PINS_RELAYS = [15, 16, 18, 22] 
PIN_BUTTON = 11                
ALL_PINS = PINS_RELAYS + [PIN_BUTTON]

session_data = {"active": False, "count": 0}

# --- INITIALIZATION ---
def init_gpio():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    for pin in PINS_RELAYS:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print("🚀 Hardware Ready at http://eco-vendo.local")

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, type TEXT, amount INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS user_balances (user_id TEXT PRIMARY KEY, points INTEGER)')
    conn.commit()
    conn.close()

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))
app.config['SECRET_KEY'] = secrets.token_hex(16)

# --- ROUTES ---
@app.route('/')
def index():
    # 1. Look for existing ID
    uid = request.cookies.get('device_id')
    
    # 2. If no ID found, create one
    if not uid:
        uid = str(uuid.uuid4())[:8]
        print(f"🆕 New User Detected: {uid}")
    else:
        print(f"👋 Returning User: {uid}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
    current_points = res['points'] if res else 0
    history = conn.execute('SELECT type, amount, timestamp FROM transactions WHERE user_id=? ORDER BY timestamp DESC LIMIT 5', (uid,)).fetchall()
    conn.close()

    response = make_response(render_template('index.html', points=current_points, history=history, device_id=uid))
    
    # --- THE FIX: COOKIE SETTINGS FOR PHONE ---
    # We set 'secure=False' because we are on HTTP
    # We set 'samesite=Lax' to allow the cookie to be sent on page loads
    response.set_cookie(
        'device_id', 
        uid, 
        max_age=30*24*60*60, # 30 Days
        path='/', 
        samesite='Lax', 
        secure=False, 
        httponly=True
    )
    return response

@app.route('/api/start_session')
def start_session():
    global session_data
    session_data.update({"active": True, "count": 0})
    return jsonify(status="success")

@app.route('/api/get_count')
def get_count():
    return jsonify(count=session_data["count"])

@app.route('/api/stop_session')
def stop_session():
    global session_data
    uid = request.cookies.get('device_id')
    new_total = 0
    if session_data["active"] and uid:
        added = session_data["count"]
        conn = sqlite3.connect(DB_PATH)
        if added > 0:
            conn.execute('INSERT INTO user_balances (user_id, points) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET points = points + ?', (uid, added, added))
            conn.execute('INSERT INTO transactions (user_id, type, amount) VALUES (?, "Deposit", ?)', (uid, added))
            conn.commit()
        res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
        new_total = res[0] if res else 0
        conn.close()
    session_data["active"] = False
    return jsonify(status="success", new_balance=new_total)

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = request.cookies.get('device_id')
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute('SELECT points FROM user_balances WHERE user_id=?', (uid,)).fetchone()
    if res and res[0] >= pts:
        conn.execute('UPDATE user_balances SET points = points - ? WHERE user_id=?', (pts, uid))
        conn.execute('INSERT INTO transactions (user_id, type, amount) VALUES (?, "Redeem", ?)', (uid, -pts))
        conn.commit()
        GPIO.output(PINS_RELAYS[slot], GPIO.LOW)
        threading.Thread(target=lambda: (time.sleep(300), GPIO.output(PINS_RELAYS[slot], GPIO.HIGH))).start()
    conn.close()
    return redirect('/')

def hardware_manager():
    global session_data
    while True:
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            if session_data["active"]:
                session_data["count"] += 1
            time.sleep(0.4)
        time.sleep(0.1)

if __name__ == '__main__':
    init_db()
    init_gpio()
    threading.Thread(target=hardware_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80, debug=False)
