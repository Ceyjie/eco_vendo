import sqlite3, time, threading, os, subprocess, signal, atexit, sys, uuid
from flask import Flask, render_template, request, redirect, jsonify, session
import OPi.GPIO as GPIO 

# --- CONFIGURATION ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")
PINS_RELAYS = [15, 16, 18, 19] # Physical Board Pins
PIN_BUTTON = 11                # Physical Board Pin

# --- FLASK APP SETUP ---
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = 'ECO_SECRET_V1_2026' # Secure session cookie key

# --- GLOBAL STATE ---
session_active = False
session_count = 0

# --- DATABASE HELPERS ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists(BASE_DIR): os.makedirs(BASE_DIR)
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    conn.execute('''CREATE TABLE IF NOT EXISTS transactions 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, type TEXT, amount INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# --- GPIO CORE ---
def init_gpio():
    print("🧹 Cleaning up GPIO...")
    for pin in [11, 15, 16, 18, 19]:
        subprocess.run(f"echo {pin} | sudo tee /sys/class/gpio/unexport", shell=True, capture_output=True)
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    for pin in PINS_RELAYS:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print("🚀 GPIO Ready!")

atexit.register(GPIO.cleanup)

# --- IDENTITY LOGIC ---
def get_uid():
    if 'uid' not in session:
        session['uid'] = str(uuid.uuid4())
    return session['uid']

# --- WEB ROUTES ---
@app.route('/')
def index():
    uid = get_uid()
    conn = get_db()
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    pts = user['points'] if user else 0
    hist = conn.execute('SELECT type, amount, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5', (uid,)).fetchall()
    conn.close()
    return render_template('index.html', points=pts, history=hist)

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
        conn = get_db()
        conn.execute('INSERT INTO users (user_id, points) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET points = points + ?', (uid, session_count, session_count))
        conn.execute('INSERT INTO transactions (user_id, type, amount) VALUES (?, "Earned", ?)', (uid, session_count))
        conn.commit()
        conn.close()
    session_active = False
    return jsonify(status="success")

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_uid()
    conn = get_db()
    user = conn.execute('SELECT points FROM users WHERE user_id = ?', (uid,)).fetchone()
    if user and user['points'] >= pts:
        conn.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (pts, uid))
        conn.execute('INSERT INTO transactions (user_id, type, amount) VALUES (?, ?, ?)', (uid, f"Slot {slot+1} On", -pts))
        conn.commit()
        conn.close()
        
        # RELAY ACTIVATION
        relay_pin = PINS_RELAYS[slot]
        GPIO.output(relay_pin, GPIO.LOW) # ON
        
        def auto_off():
            time.sleep(300) # 5 Minutes
            GPIO.output(relay_pin, GPIO.HIGH) # OFF
        threading.Thread(target=auto_off, daemon=True).start()
        
        return redirect('/')
    conn.close()
    return "Insufficient Points", 403

# --- HARDWARE THREAD ---
def hardware_manager():
    global session_count
    while True:
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            if session_active:
                session_count += 1
                print(f"Bottle Detected! Session: {session_count}")
            time.sleep(0.4) # Debounce
        time.sleep(0.1)

if __name__ == '__main__':
    init_db()
    init_gpio()
    threading.Thread(target=hardware_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
