import time, threading, os, json, uuid
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response

# --- HARDWARE CONFIG ---
# These are the "Physical" GPIO numbers for Orange Pi One
PIN_IR_BOTTOM = 6
PIN_IR_TOP = 1
PIN_BUZZER = 0
PIN_BTN_START = 13
PINS_RELAYS = [3, 2, 67, 21] 
DB_FILE = "eco_database.json"

app = Flask(__name__)

# --- GLOBAL STATE ---
session_data = {"state": "IDLE", "count": 0, "active_user": None, "last_activity": time.time()}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- SYSTEM HELPERS ---
def gpio_init():
    for pin in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BUZZER] + PINS_RELAYS:
        # Export pin if not already exported
        if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
            os.system(f"echo {pin} > /sys/class/gpio/export")
        time.sleep(0.1)
        # Set directions
        mode = "out" if pin in [PIN_BUZZER] + PINS_RELAYS else "in"
        os.system(f"echo {mode} > /sys/class/gpio/gpio{pin}/direction")
    print("✅ GPIO Initialized Successfully")

def gpio_write(pin, val):
    os.system(f"echo {val} > /sys/class/gpio/gpio{pin}/value")

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except: return 1

# --- RELAY ENGINE ---
def relay_worker(slot):
    gpio_write(PINS_RELAYS[slot], 1) # High to turn on relay
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] -= 1
    gpio_write(PINS_RELAYS[slot], 0) # Low to turn off

def start_relay(slot, sec):
    is_new = slot_status[slot] == 0
    slot_status[slot] += sec
    if is_new:
        threading.Thread(target=relay_worker, args=(slot,), daemon=True).start()

# --- WEB ROUTES ---
@app.route('/')
def index():
    uid = request.cookies.get('user_uuid') or str(uuid.uuid4())[:8]
    # Point loading logic here...
    resp = make_response(render_template('index.html', device_id=uid, points=10, logs=[]))
    resp.set_cookie('user_uuid', uid)
    return resp

@app.route('/api/status')
def get_status():
    uid = request.cookies.get('user_uuid')
    return jsonify({
        "state": session_data["state"],
        "session": session_data["count"],
        "slots": [slot_status[i] for i in range(4)],
        "is_my_session": session_data["active_user"] == uid
    })

@app.route('/api/start_session')
def web_start():
    uid = request.cookies.get('user_uuid')
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": uid})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def web_stop():
    session_data.update({"state": "IDLE", "active_user": None})
    return jsonify({"status": "ok"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    start_relay(slot, pts * 300) # 5 mins per point
    return redirect(url_for('index'))

# --- HARDWARE THREAD ---
def hw_loop():
    while True:
        if session_data["state"] == "INSERTING":
            # Logic: Both IR sensors must be blocked (0) to count a bottle
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                os.system(f"echo 1 > /sys/class/gpio/gpio{PIN_BUZZER}/value")
                time.sleep(0.2)
                os.system(f"echo 0 > /sys/class/gpio/gpio{PIN_BUZZER}/value")
                time.sleep(0.5) # Anti-double count
        time.sleep(0.1)

if __name__ == '__main__':
    gpio_init()
    threading.Thread(target=hw_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=80, debug=False)
