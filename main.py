import sqlite3, time, threading, os, subprocess
import signal
import atexit
import sys
from flask import Flask, render_template, request, redirect, jsonify
from RPLCD.i2c import CharLCD
import OPi.GPIO as GPIO 

# --- SETTINGS FOR USER "ECO" ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")

# --- GPIO SETUP ---
# ====================== SAFE & BULLETPROOF GPIO SETUP ======================
def gpio_cleanup():
    try:
        GPIO.cleanup()
        print("🧹 GPIO cleaned up successfully - ready for next run")
    except:
        pass

# Auto-clean on Ctrl+C, kill, or shutdown
atexit.register(gpio_cleanup)
signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

GPIO.setwarnings(False)
GPIO.cleanup()
GPIO.setmode(GPIO.BOARD)


PINS_RELAYS = [15, 16, 18, 19]
PIN_BUTTON = 11
PIN_EXTRA  = 21

# Setup relays (HIGH = OFF = safe for vending/charging)
for pin in PINS_RELAYS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

GPIO.setup(PIN_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)   # reliable button
GPIO.setup(PIN_EXTRA, GPIO.OUT, initial=GPIO.LOW)

print("✅ GPIO initialized successfully (BOARD mode)")
print(f"   Relays: {PINS_RELAYS}")
print(f"   Button: {PIN_BUTTON} (pull-up enabled)")
print(f"   Extra : {PIN_EXTRA}")
# ==========================================================================

# --- LCD Setup (unchanged) ---
try:
    lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4)
except:
    lcd = CharLCD('PCF8574', 0x27, port=1, cols=20, rows=4)



app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
slot_timers = [0, 0, 0, 0]
session_active = False
session_bottles = 0

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (mac TEXT PRIMARY KEY, points INTEGER DEFAULT 0)')
    conn.commit()
    conn.close()

# ... (rest of the logic remains the same) ...

if __name__ == '__main__':
    init_db()
    threading.Thread(target=hardware_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
