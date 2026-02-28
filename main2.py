import sqlite3
import time
import threading
import os
import subprocess
import signal
import atexit
import sys
from flask import Flask, render_template, request, redirect, jsonify
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

    # Setup Relays: HIGH = OFF (Relay modules are usually active-low)
    for pin in PINS_RELAYS:
        try:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
            print(f"✅ Relay Pin {pin} initialized.")
        except Exception as e:
            print(f"⚠️ Error on Pin {pin}: {e}")

    GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_EXTRA, GPIO.OUT, initial=GPIO.LOW)
    print("🚀 GPIO Setup Complete!")

# --- DATABASE SETUP ---
def init_db():
    if not os.path.exists(BASE_DIR):
        os.makedirs(BASE_DIR)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (mac TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    conn.commit()
    conn.close()

# --- FLASK WEB SERVER ---
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

@app.route('/')
def index():
    return render_template('index.html', points=user_points)

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
    global session_data, user_points
    session_data["active"] = False
    user_points += session_data["count"]
    print(f"🏁 Session Stopped: Saved {session_data['count']} points. Total: {user_points}")
    return jsonify(status="success")

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    global user_points
    if user_points >= pts:
        user_points -= pts
        relay_pin = PINS_RELAYS[slot]
        
        # Turn relay ON (Active Low)
        GPIO.output(relay_pin, GPIO.LOW)
        print(f"⚡ Slot {slot} (Pin {relay_pin}) activated for 5 minutes!")
        
        # Start a thread to turn the relay off after 300 seconds (5 mins)
        def turn_off():
            time.sleep(300)
            GPIO.output(relay_pin, GPIO.HIGH)
            print(f"🔌 Slot {slot} deactivated.")
            
        threading.Thread(target=turn_off).start()
        return redirect('/')
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
