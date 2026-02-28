import sqlite3
import time
import threading
import os
import subprocess
import signal
import atexit
import sys
from flask import Flask, render_template, request, redirect, jsonify, make_response
import OPi.GPIO as GPIO 

# --- SETTINGS ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")

# --- GPIO DEFINITIONS (Physical BOARD Numbers) ---
PINS_RELAYS = [15, 16, 18, 19] # USB 1, USB 2, USB 3, AC 220V
PIN_BUTTON = 11                # The physical button for bottle counting
PIN_EXTRA  = 21
ALL_PINS = PINS_RELAYS + [PIN_BUTTON, PIN_EXTRA]

# --- GLOBAL STATE ---
session_data = {"active": False, "count": 0}
# Note: In a production version, you would load/save user_points from DB_PATH
user_points = 0 

# --- GPIO INITIALIZATION ---
def force_release_pins():
    """Forces the kernel to release pins to avoid 'Device or resource busy' errors."""
    print("🧹 Forcefully releasing GPIO pins...")
    for pin in ALL_PINS:
        try:
            subprocess.run(f"echo {pin} | sudo tee /sys/class/gpio/unexport", 
                           shell=True, capture_output=True, text=True)
        except Exception:
            pass
    time.sleep(0.5)

def gpio_cleanup():
    try:
        GPIO.cleanup()
        print("🧹 GPIO cleaned up successfully.")
    except:
        pass

atexit.register(gpio_cleanup)
signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

def init_gpio():
    force_release_pins()
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)

    # Forcefully setup EVERY Relay Pin in the list
    for pin in PINS_RELAYS:
        try:
            # Tell the Orange Pi this pin is an OUTPUT
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
            print(f"✅ Hardware Ready: Relay Pin {pin} (Slot {PINS_RELAYS.index(pin)+1})")
        except Exception as e:
            print(f"❌ Hardware Error: Pin {pin} failed: {e}")

    # Setup the Bottle Counter Button
    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_EXTRA, GPIO.OUT, initial=GPIO.LOW)
    print("🚀 All Hardware Channels Configured!")


# --- DATABASE SETUP ---
def init_db():
    if not os.path.exists(BASE_DIR):
        os.makedirs(BASE_DIR)
    conn = sqlite3.connect(DB_PATH)
    # Create transactions table for history
    conn.execute('''CREATE TABLE IF NOT EXISTS transactions 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                     type TEXT, 
                     amount INTEGER, 
                     timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# --- FLASK WEB SERVER ---
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
static_folder=os.path.join(BASE_DIR, "static"))

from flask import make_response # Add this to your imports at the top

@app.route('/')
def index():
    # 1. Try to get points from the browser cookie
    # We use 0 as the default if the cookie isn't found
    saved_points = request.cookies.get('user_points', '0')
    
    # 2. Get history from DB (same as before)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    history = conn.execute('SELECT type, amount, timestamp FROM transactions ORDER BY timestamp DESC LIMIT 5').fetchall()
    conn.close()
    
    # 3. Create the response and "bake" the cookie into it
    response = make_response(render_template('index.html', points=saved_points, history=history))
    
    # We set the cookie to last for 30 days
    response.set_cookie('user_points', saved_points, max_age=30*24*60*60)
    return response

@app.route('/api/start_session')
def start_session():
    global session_data
    session_data["active"] = True
    session_data["count"] = 0
    print("🔋 Session Started: Waiting for bottles...")
    return jsonify(status="success")

@app.route('/api/get_count')
def get_count():
    return jsonify(count=session_data["count"])

@app.route('/api/stop_session')
def stop_session():
    global session_data
    added_points = session_data["count"]
    
    # Get current points from the cookie
    current_points = int(request.cookies.get('user_points', '0'))
    new_total = current_points + added_points

    # Log to DB History
    if added_points > 0:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('INSERT INTO transactions (type, amount) VALUES (?, ?)',
                     ("Bottle Deposit", added_points))
        conn.commit()
        conn.close()

    session_data["active"] = False
    
    # Send the new total back to the frontend
    response = jsonify(status="success", new_points=new_total)
    response.set_cookie('user_points', str(new_total), max_age=30*24*60*60)
    return response


from flask import make_response # Ensure this is in your imports

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    # 1. Get current points from the browser cookie (default to 0)
    current_points = int(request.cookies.get('user_points', '0'))
    
    if current_points >= pts:
        # 2. Calculate new balance
        new_total = current_points - pts

        # 3. Log to Database History (for the dashboard table)
        try:
            conn = sqlite3.connect(DB_PATH)
            slot_names = ["USB 1", "USB 2", "USB 3", "AC 220V"]
            label = slot_names[slot] if slot < len(slot_names) else f"Slot {slot+1}"
            conn.execute('INSERT INTO transactions (type, amount) VALUES (?, ?)',
                         (f"Used {label}", -pts))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Database logging error: {e}")

        # 4. Hardware Control (Relay ON)
        relay_pin = PINS_RELAYS[slot]
        GPIO.output(relay_pin, GPIO.LOW)

        def turn_off():
            time.sleep(300) # 5 minutes
            GPIO.output(relay_pin, GPIO.HIGH)

        threading.Thread(target=turn_off).start()

        # 5. Save the NEW balance back to the browser cookie
        response = redirect('/')
        response.set_cookie('user_points', str(new_total), max_age=30*24*60*60) # Lasts 30 days
        return response
    else:
        return "Insufficient Points!", 403


# --- HARDWARE MANAGER ---
def hardware_manager():
    global session_data
    print("🤖 Hardware manager active...")
    while True:
        # Detect button press (Active Low)
        if GPIO.input(PIN_BUTTON) == GPIO.LOW:
            if session_data["active"]:
                session_data["count"] += 1
                print(f"🟢 Bottle Detected! Total: {session_data['count']}")
            else:
                print("🔘 Button pressed, but no session is active.")
            
            # Debounce delay to prevent multiple counts for one press
            time.sleep(0.4)
        
        time.sleep(0.1)

# --- MAIN START ---
if __name__ == '__main__':
    init_db()
    init_gpio()
    
    # Start background hardware thread
    threading.Thread(target=hardware_manager, daemon=True).start()
    
    # Run server on Port 80
    print("🌐 Visit http://192.168.254.181 on your laptop")
    app.run(host='0.0.0.0', port=80)
