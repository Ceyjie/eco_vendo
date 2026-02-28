import sqlite3, time, threading, os, subprocess, signal, atexit, sys, uuid
from flask import Flask, render_template, request, redirect, jsonify, session
import OPi.GPIO as GPIO 

# 1. --- SETTINGS & PATHS ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")
PINS_RELAYS = [15, 16, 18, 19]
PIN_BUTTON = 11
ALL_PINS = PINS_RELAYS + [PIN_BUTTON]

# 2. --- INITIALIZE FLASK APP (Must be before @app.route) ---
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = 'ECO_VENDO_SUPER_SECRET_KEY'

# 3. --- GLOBAL STATE ---
session_active = False
session_count = 0

# 4. --- DATABASE LOGIC ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    conn.execute('''CREATE TABLE IF NOT EXISTS transactions 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                     user_id TEXT, 
                     type TEXT, 
                     amount INTEGER, 
                     timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_user_points(uid):
    conn = get_db()
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    conn.close()
    return user['points'] if user else 0

def update_user_points(uid, add_points):
    conn = get_db()
    conn.execute('''INSERT INTO users (user_id, points) VALUES (?, ?) 
                    ON CONFLICT(user_id) DO UPDATE SET points = points + ?''', (uid, add_points, add_points))
    conn.commit()
    conn.close()

def log_transaction(uid, t_type, amount):
    conn = get_db()
    conn.execute('INSERT INTO transactions (user_id, type, amount) VALUES (?, ?, ?)', (uid, t_type, amount))
    conn.commit()
    conn.close()

def get_history(uid):
    conn = get_db()
    history = conn.execute('SELECT type, amount, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5', (uid,)).fetchall()
    conn.close()
    return history

# 5. --- GPIO CORE ---
def init_gpio():
    for pin in ALL_PINS:
        subprocess.run(f"echo {pin} | sudo tee /sys/class/gpio/unexport", shell=True, capture_output=True)
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    for pin in PINS_RELAYS:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)

atexit.register(GPIO.cleanup)

# 6. --- WEB ROUTES ---
def get_uid():
    if 'uid' not in session:
        session['uid'] = str(uuid.uuid4())
    return session['uid']

@app.route('/')
def index():
    uid = get_uid()
    points = get_user_points(uid)
    history = get_history(uid)
    return render_template('index.html', points=points, history=history)

@app.route('/api/start_session')
def start_session():
    global session_active, session_count
    session_active = True
    session_count = 0
    return jsonify(status="success")

@app.route('/api/get_count')
def get_count():
    return jsonify(count=session_count)

@app.route('/api/stop_session')
def stop_session():
    global session_active, session_count
    if session_active and session_count > 0:
        uid = get_uid()
        update_user_points(uid, session_count)
        log_transaction(uid, "Earned", session_count)
        session_active = False
    return jsonify(status="success")

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_uid()
    if get_user_points(uid) >= pts:
        update_user_points(uid, -pts)
        log_transaction(uid, f"Redeemed Slot {slot+1}", -pts)
        
        relay_pin = PINS_RELAYS[slot]
        GPIO.output(relay_pin, GPIO.LOW) # Turn ON
        
        def turn_off():
            time.sleep(300) # 5 minutes
            GPIO.output(relay_pin, GPIO.HIGH)
        threading.Thread(target=turn_off, daemon=True).start()
        
        return redirect('/')
    return "Insufficient Points", 403

# 7. --- HARDWARE BACKGROUND THREAD ---
def hardware_manager():
    global session_count
    while True:
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            if session_active:
                session_count += 1
            time.sleep(0.4)
        time.sleep(0.1)

# 8. --- MAIN EXECUTION ---
if __name__ == '__main__':
    init_db()
    init_gpio()
    threading.Thread(target=hardware_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
