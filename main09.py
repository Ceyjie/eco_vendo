import time, threading, os, subprocess, json, uuid
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"] 
DB_FILE = "eco_database.json"
ADMIN_PASSWORD = "1234"

app = Flask(__name__)

# --- STATE ---
session_data = {"state": "IDLE", "count": 0, "active_user": None, "last_activity": time.time()}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# --- GPIO SETUP ---
def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"{path}/value", "w") as f: f.write(value)

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# --- RELAY ENGINE ---
def start_relay(slot, sec):
    is_new = slot_status[slot] == 0
    slot_status[slot] += sec
    if is_new:
        threading.Thread(target=relay_worker, args=(slot,), daemon=True).start()

def relay_worker(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] -= 1
    gpio_write(PINS_RELAYS[slot], 0)

# --- ROUTES ---
@app.route('/')
def index():
    db = load_db()
    uid = request.cookies.get('user_uuid') or str(uuid.uuid4())[:8]
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    resp = make_response(render_template('index.html', device_id=uid, points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

@app.route('/api/status')
def get_status():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    return jsonify({
        "state": session_data["state"],
        "session": session_data["count"],
        "points": db["users"].get(uid, {"points": 0})["points"],
        "slots": [slot_status[i] for i in range(4)],
        "is_my_session": session_data["active_user"] == uid
    })

@app.route('/api/admin_stats')
def admin_stats():
    db = load_db()
    # Matches your JS: u.user_id and u.points
    user_list = [{"user_id": k, "points": v["points"]} for k, v in db["users"].items()]
    return jsonify({"total_bottles": db.get("total_bottles", 0), "users": user_list})

@app.route('/api/emergency_reset')
def emergency_reset():
    for pin in PINS_RELAYS: gpio_write(pin, 0)
    for i in range(4): slot_status[i] = 0
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    return jsonify({"status": "reset"})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = request.cookies.get('user_uuid')
    db = load_db()
    if db["users"].get(uid, {}).get("points", 0) >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", f"Slot {slot+1}"])
        save_db(db)
        start_relay(slot, pts * 300)
    return redirect(url_for('index'))

@app.route('/api/start_session')
def start_session():
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0, "active_user": request.cookies.get('user_uuid')})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def stop_session():
    uid = request.cookies.get('user_uuid')
    if session_data["active_user"] == uid:
        db = load_db()
        added = session_data["count"]
        db["users"][uid]["points"] += added
        db["total_bottles"] = db.get("total_bottles", 0) + added
        db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"])
        save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    for p in PINS_RELAYS: gpio_setup(p, "out")
    gpio_setup(PIN_BUZZER, "out")
    app.run(host='0.0.0.0', port=80)
