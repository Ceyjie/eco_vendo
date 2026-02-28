import sqlite3, time, threading, os, subprocess
from flask import Flask, render_template, request, redirect, jsonify
from RPLCD.i2c import CharLCD
import OPi.GPIO as GPIO 

# --- SETTINGS FOR USER "ECO" ---
BASE_DIR = "/home/eco/eco_vendo"
DB_PATH = os.path.join(BASE_DIR, "eco_charge.db")

# --- GPIO SETUP ---
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BOARD)

PINS_RELAYS = [15, 16, 18, 19]
for pin in PINS_RELAYS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

GPIO.setup(11, GPIO.IN) 
GPIO.setup(21, GPIO.OUT, initial=GPIO.LOW) 

# LCD Setup (Using Bus 0 or 1 depending on your Armbian version)
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
