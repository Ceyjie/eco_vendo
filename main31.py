import time, threading, os, subprocess, json, uuid, signal
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory
from RPLCD.i2c import CharLCD

# ═══════════════════════════════════════════════════════════
# PIN CONFIG
# ═══════════════════════════════════════════════════════════
PIN_IR_BOTTOM, PIN_IR_TOP              = "6", "1"
PIN_BUZZER                             = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS                            = ["3", "2", "67", "21"]
PIN_SERVO                              = "20"   # PA20 - Pin 38
PIN_HX711_DOUT                         = "68"   # PC4  - Pin 16
PIN_HX711_SCK                          = "71"   # PC7  - Pin 18

SLOT_NAMES     = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE        = "eco_database.json"
ADMIN_PASSWORD = "1234"

# ═══════════════════════════════════════════════════════════
# BOTTLE WEIGHT RANGES
# ═══════════════════════════════════════════════════════════
# Small bottle   : 9–16g   → 1 pt
# Nature Spring  : 16–19g  → 2 pts
# Big bottle     : 29–39g  → 2 pts
WEIGHT_MIN_VALID  =  8
WEIGHT_SMALL_MIN  =  9
WEIGHT_SMALL_MAX  = 16
WEIGHT_NATURE_MIN = 16
WEIGHT_NATURE_MAX = 19
WEIGHT_BIG_MIN    = 29
WEIGHT_BIG_MAX    = 39

# ═══════════════════════════════════════════════════════════
# HX711 STATE
# ═══════════════════════════════════════════════════════════
hx_calibration = 441.17
hx_tare        = 0.0
hx_fd_dout     = None
hx_fd_sck      = None
hx_ready       = False

# ═══════════════════════════════════════════════════════════
# SERVO STATE
# ═══════════════════════════════════════════════════════════
servo_deg     = 90   # 90=home, 0=push
servo_active  = True

# ═══════════════════════════════════════════════════════════
# SYSTEM STATE
# ═══════════════════════════════════════════════════════════
session_data = {
    "state": "IDLE",
    "count": 0,
    "active_user": None,
    "selected_slot": 0,
    "add_time_choice": 1,
    "last_activity": time.time(),
    "last_bottle_msg": ""
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

admin_data = {
    "active": False,
    "user_index": 0,
    "hold_start": 0,
    "holding": False
}

# bottle processing lock — prevents double trigger
bottle_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════
def load_db():
    if not os.path.exists(DB_FILE):
        return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# ═══════════════════════════════════════════════════════════
# GPIO
# ═══════════════════════════════════════════════════════════
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

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

def beep(times=1):
    def run():
        for _ in range(times):
            gpio_write(PIN_BUZZER, 1); time.sleep(0.08)
            gpio_write(PIN_BUZZER, 0); time.sleep(0.04)
    threading.Thread(target=run, daemon=True).start()

def beep_now(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.08)
        gpio_write(PIN_BUZZER, 0); time.sleep(0.04)

# ═══════════════════════════════════════════════════════════
# LCD
# ═══════════════════════════════════════════════════════════
lcd = None
current_lcd_lines = ["", "", "", ""]

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear()
            return
        except: lcd = None

def lcd_write(new_lines):
    global current_lcd_lines, lcd
    if not lcd:
        init_lcd()
        return
    try:
        safe_lines = []
        for line in new_lines:
            cleaned = ""
            for ch in str(line):
                if ch.isascii() and ch not in ('%', '\n', '\r', '\x00'):
                    cleaned += ch
                else:
                    cleaned += ' '
            safe_lines.append(cleaned.ljust(20)[:20])
        for i, line in enumerate(safe_lines):
            if line != current_lcd_lines[i]:
                lcd.cursor_pos = (i, 0)
                lcd.write_string(line)
                current_lcd_lines[i] = line
    except Exception as e:
        print(f"LCD error: {e}")
        lcd = None
        current_lcd_lines = ["", "", "", ""]
        init_lcd()

def format_time(seconds):
    seconds = max(0, seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}" if m < 60 else f"{m//60:02d}:{m%60:02d}"

# ═══════════════════════════════════════════════════════════
# HX711
# ═══════════════════════════════════════════════════════════
def hx_begin():
    global hx_fd_dout, hx_fd_sck
    gpio_setup(PIN_HX711_DOUT, "in")
    gpio_setup(PIN_HX711_SCK,  "out", "0")
    hx_fd_dout = os.open(f"/sys/class/gpio/gpio{PIN_HX711_DOUT}/value", os.O_RDONLY)
    hx_fd_sck  = os.open(f"/sys/class/gpio/gpio{PIN_HX711_SCK}/value",  os.O_WRONLY)

def hx_dout():
    os.lseek(hx_fd_dout, 0, 0)
    return os.read(hx_fd_dout, 1) == b'0'

def hx_sck(val):
    os.lseek(hx_fd_sck, 0, 0)
    os.write(hx_fd_sck, b'1' if val else b'0')

def hx_read_raw():
    deadline = time.time() + 0.5
    while not hx_dout():
        if time.time() > deadline: return None
    raw = 0
    for _ in range(24):
        hx_sck(1)
        bit = 0 if hx_dout() else 1
        hx_sck(0)
        raw = (raw << 1) | bit
    hx_sck(1); hx_sck(0)
    if raw & 0x800000: raw -= 0x1000000
    return raw

def hx_get_grams():
    global hx_calibration
    readings = []
    for _ in range(3):
        r = hx_read_raw()
        if r is not None:
            readings.append((r - hx_tare) / hx_calibration)
    if not readings: return None
    readings.sort()
    result = readings[len(readings) // 2]
    if result < -2.0:
        hx_calibration = -abs(hx_calibration)
        result = -result
    return result

def hx_auto_zero():
    global hx_tare, hx_ready
    print("HX711 zeroing...", end="", flush=True)
    while True:
        total, count = 0, 0
        for _ in range(50):
            r = hx_read_raw()
            if r is not None:
                total += r
                count += 1
            print(".", end="", flush=True)
        print()
        if count == 0:
            print("No samples, retrying...")
            continue
        hx_tare = total / count
        # Verify
        verify = []
        for _ in range(10):
            r = hx_read_raw()
            if r is not None:
                verify.append((r - hx_tare) / hx_calibration)
        if not verify:
            print("Verify failed, retrying...")
            continue
        verify.sort()
        check = abs(verify[len(verify) // 2])
        if check <= 5.0:
            print(f"Zero OK. Offset={hx_tare:.0f}")
            hx_ready = True
            return
        else:
            print(f"Abnormal ({check:.1f}g), re-zeroing...")

# ═══════════════════════════════════════════════════════════
# SERVO
# ═══════════════════════════════════════════════════════════
def servo_pwm_thread():
    fd = os.open(f"/sys/class/gpio/gpio{PIN_SERVO}/value", os.O_WRONLY)
    while servo_active:
        deg      = servo_deg
        pulse_ms = 1.0 + (deg / 180.0) * 1.0
        period   = 0.02
        os.lseek(fd, 0, 0); os.write(fd, b'1')
        time.sleep(pulse_ms / 1000.0)
        os.lseek(fd, 0, 0); os.write(fd, b'0')
        time.sleep(period - pulse_ms / 1000.0)
    os.close(fd)

def servo_goto(deg, hold=0.6):
    global servo_deg
    servo_deg = max(0, min(180, deg))
    time.sleep(hold)

# ═══════════════════════════════════════════════════════════
# BOTTLE CLASSIFICATION
# ═══════════════════════════════════════════════════════════
def classify_bottle(weight_g):
    """Classify by weight only. IR is only used to trigger detection."""
    if weight_g < WEIGHT_MIN_VALID:
        return 0, None, "   NOT A BOTTLE!    ", "   Bottles only!    "
    elif WEIGHT_SMALL_MIN <= weight_g <= WEIGHT_SMALL_MAX:
        return 1, "SMALL",  "  SMALL BOTTLE      ", "     +1 POINT!      "
    elif WEIGHT_NATURE_MIN <= weight_g <= WEIGHT_NATURE_MAX:
        return 2, "NATURE", "  NATURE SPRING     ", "     +2 POINTS!     "
    elif WEIGHT_BIG_MIN <= weight_g <= WEIGHT_BIG_MAX:
        return 2, "BIG",    "  BIG BOTTLE        ", "     +2 POINTS!     "
    else:
        return 0, None,     "  INVALID BOTTLE!   ", f"  {weight_g:.0f}g not accepted  "[:20]

# ═══════════════════════════════════════════════════════════
# BOTTLE PROCESSING
# ═══════════════════════════════════════════════════════════
def process_bottle():
    """Triggered by IR — weight only classification"""
    with bottle_lock:

        # Immediately start sampling weight
        # Show live weight on LCD during sampling
        lcd_write([
            "   DETECTED!        ",
            "   Weighing...      ",
            "   Place bottle on  ",
            "   sensor steadily  "
        ])

        samples    = []
        start_time = time.time()
        timeout    = 5.0   # max wait

        while time.time() - start_time < timeout:
            elapsed = time.time() - start_time
            if hx_ready:
                w = hx_get_grams()
                if w is not None and w > WEIGHT_MIN_VALID:
                    samples.append(w)

            # Show live feedback
            if samples:
                avg = sum(samples) / len(samples)
                lcd_write([
                    "   WEIGHING...      ",
                    f"   {avg:6.1f} g        "[:20],
                    f"   Samples: {len(samples)}/5     "[:20],
                    "   Hold steady...   "
                ])

            # Once we have 5 valid stable samples — confirm
            if len(samples) >= 5:
                samples_sorted = sorted(samples[-5:])
                variance = samples_sorted[-1] - samples_sorted[0]
                if variance <= 5.0:
                    break   # stable reading confirmed

            time.sleep(0.4)

        # Not enough samples
        if len(samples) < 3:
            lcd_write([
                "   NO BOTTLE        ",
                "   DETECTED!        ",
                "   Place bottle on  ",
                "   sensor & retry   "
            ])
            beep_now(3)
            time.sleep(2)
            return

        # Use median of last 5 samples
        final = sorted(samples[-5:]) if len(samples) >= 5 else sorted(samples)
        weight   = final[len(final) // 2]
        variance = final[-1] - final[0]

        # Unstable
        if variance > 5.0:
            lcd_write([
                "   HOLD STILL!      ",
                "   Keep bottle firm ",
                f"   Variance:{variance:.1f}g    "[:20],
                "   Try again...     "
            ])
            time.sleep(2)
            return

        pts, label, line1, line2 = classify_bottle(weight)
        print(f"Bottle: {weight:.1f}g both_ir={both_ir} pts={pts} label={label}")

        if pts > 0:
            # Valid bottle
            lcd_write([
                "    ACCEPTED!       ",
                line1,
                line2,
                "   Processing...    "
            ])
            time.sleep(0.5)
            servo_goto(0, hold=1.0)   # push to 0deg

            session_data["count"]          += pts
            session_data["last_activity"]   = time.time()
            session_data["last_bottle_msg"] = line1.strip()
            beep_now(pts)

            # Save points to DB
            db = load_db()
            if session_data["active_user"] and session_data["active_user"] != "LOCAL_USER":
                uid = session_data["active_user"]
                if uid in db["users"]:
                    db["users"][uid]["points"] += pts
            db["total_bottles"] += 1
            db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label or "Bottle"])
            save_db(db)

            # Wait for bottle removal
            lcd_write([
                "   REMOVE BOTTLE    ",
                "   FROM SENSOR...   ",
                "                    ",
                "                    "
            ])
            deadline = time.time() + 6.0
            while time.time() < deadline:
                w = hx_get_grams()
                if w is not None and w < WEIGHT_MIN_VALID:
                    break
                time.sleep(0.2)

            # Servo back home
            servo_goto(90, hold=0.5)

            # Show result
            total = session_data["count"]
            lcd_write([
                line1,
                line2,
                f"   Total: {total} pts      "[:20],
                "   Keep recycling!  "
            ])
            time.sleep(2)

        else:
            # Invalid
            lcd_write([
                "   INVALID ITEM!    ",
                line1,
                line2,
                "   Try again...     "
            ])
            beep_now(3)
            time.sleep(2)

# ═══════════════════════════════════════════════════════════
# RELAY
# ═══════════════════════════════════════════════════════════
def start_or_extend_relay(slot, additional_seconds):
    is_already_running = slot_status[slot] > 0
    slot_status[slot] += additional_seconds
    if not is_already_running:
        threading.Thread(target=run_relay_thread, args=(slot,), daemon=True).start()

def run_relay_thread(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1)
        slot_status[slot] = max(0, slot_status[slot] - 1)
    gpio_write(PINS_RELAYS[slot], 0)

# ═══════════════════════════════════════════════════════════
# SHUTDOWN
# ═══════════════════════════════════════════════════════════
def shutdown(signum=None, frame=None):
    global servo_active
    print("\nShutting down...")
    servo_active = False
    for pin in PINS_RELAYS: gpio_write(pin, 0)
    gpio_write(PIN_BUZZER, 0)
    gpio_write(PIN_SERVO,  0)
    try:
        if lcd:
            lcd.clear()
            lcd.backlight_enabled = False
            lcd.close()
    except: pass
    print("All outputs off. Bye.")
    os._exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ═══════════════════════════════════════════════════════════
# BUTTON HANDLERS
# ═══════════════════════════════════════════════════════════
def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]

    if admin_data["active"]:
        if pin == PIN_BTN_SELECT:
            db = load_db()
            users = list(db["users"].items())
            if users:
                admin_data["user_index"] = (admin_data["user_index"] + 1) % len(users)
            beep(1)
        elif pin == PIN_BTN_START:
            system_refresh()
        elif pin == PIN_BTN_CONFIRM:
            admin_data["active"] = False
            session_data.update({"state": "IDLE", "count": 0, "active_user": None})
            beep(2)
        return

    if pin == PIN_BTN_START and s == "IDLE":
        beep(1)
        session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL_USER"})
    elif pin == PIN_BTN_SELECT:
        if s == "SELECTING":
            beep(1)
            session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        elif s == "ADD_TIME_PROMPT":
            beep(1)
            session_data["add_time_choice"] = 1 - session_data["add_time_choice"]
    elif pin == PIN_BTN_CONFIRM:
        if s == "INSERTING":
            beep(1)
            session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        elif s == "SELECTING":
            beep(1)
            if slot_status[session_data["selected_slot"]] > 0:
                session_data["state"] = "ADD_TIME_PROMPT"
            else:
                finalize_transaction()
        elif s == "ADD_TIME_PROMPT":
            if session_data["add_time_choice"] == 1:
                finalize_transaction()
            else:
                beep(1)
                session_data["state"] = "SELECTING"

def finalize_transaction():
    slot = session_data["selected_slot"]
    pts  = session_data["count"]
    beep(2)
    start_or_extend_relay(slot, pts * 300)
    session_data["state"] = "THANK_YOU"
    lcd_write(["     THANK YOU!     ", " You helped protect ", " our environment by ", " recycling plastic! "])
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0, "active_user": None})).start()

# ═══════════════════════════════════════════════════════════
# HARDWARE LOOP
# ═══════════════════════════════════════════════════════════
def hardware_loop():
    btn_pins    = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val    = {p: 1   for p in btn_pins}
    last_press  = {p: 0.0 for p in btn_pins}
    ir_cooldown = 0
    ir_last_bot = 1

    while True:
        now = time.time()
        start_held  = gpio_read(PIN_BTN_START)  == 0
        select_held = gpio_read(PIN_BTN_SELECT) == 0
        both_held   = start_held and select_held

        # Admin hold entry
        if both_held:
            if not admin_data["holding"]:
                admin_data["holding"]    = True
                admin_data["hold_start"] = now
            elif (now - admin_data["hold_start"]) >= 10.0 and not admin_data["active"]:
                admin_data["active"]     = True
                admin_data["user_index"] = 0
                admin_data["holding"]    = False
                for p in btn_pins:
                    last_val[p]   = 0
                    last_press[p] = now
                beep(3)
        else:
            admin_data["holding"] = False

        # Buttons
        if not both_held:
            for p in btn_pins:
                val = gpio_read(p)
                if val == 0 and last_val[p] == 1:
                    if (now - last_press[p]) > 0.3:
                        last_press[p] = now
                        handle_physical_press(p)
                last_val[p] = val
        else:
            for p in btn_pins:
                last_val[p]   = 0
                last_press[p] = now

        # IR sensor — triggers bottle processing in background
        if session_data["state"] == "INSERTING" and now >= ir_cooldown \
                and not admin_data["active"] and not bottle_lock.locked():
            bot = gpio_read(PIN_IR_BOTTOM)
            bot_triggered = (bot == 0 and ir_last_bot == 1)

            if bot_triggered:
                ir_cooldown = now + 6.0
                threading.Thread(
                    target=process_bottle,
                    daemon=True
                ).start()

            ir_last_bot = bot

        # Auto timeout 20s
        if not admin_data["active"]:
            if session_data["state"] not in ["IDLE", "THANK_YOU"]:
                if (now - session_data["last_activity"]) > 20:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
        else:
            session_data["last_activity"] = now

        time.sleep(0.01)

# ═══════════════════════════════════════════════════════════
# DISPLAY MANAGER
# ═══════════════════════════════════════════════════════════
def display_manager():
    while True:
        # Skip if bottle processing is happening (it controls LCD)
        if bottle_lock.locked():
            time.sleep(0.1)
            continue

        s = session_data["state"]

        if admin_data["active"]:
            db    = load_db()
            users = list(db["users"].items())
            total = db.get("total_bottles", 0)
            if users:
                idx = admin_data["user_index"] % len(users)
                uid, udata = users[idx]
                pts = udata.get("points", 0)
                lcd_write(["  ** ADMIN MODE **  ", f"ID:{uid[:12]}", f"PTS:{pts}  {idx+1}/{len(users)}", "B1:RST B2:NXT B3:EXIT"])
            else:
                lcd_write(["  ** ADMIN MODE **  ", f" TOTAL:{total} bottles", "   No users yet", "B1:RST       B3:EXIT"])
            time.sleep(0.2)
            continue

        if s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lcd_write(["      ECO VENDO", "     PRESS START", t1, t2])
        elif s == "INSERTING":
            lcd_write(["   INSERT BOTTLE",
                       f"   BOTTLES: {session_data['count']}",
                       f"   TIME: {session_data['count']*5}m",
                       "B3:CONFIRM"])
        elif s == "SELECTING":
            lcd_write(["      SELECT",
                       f"    > {SLOT_NAMES[session_data['selected_slot']]}",
                       f"    FOR {session_data['count']*5} MINS",
                       "B3:CONFIRM"])
        elif s == "ADD_TIME_PROMPT":
            ch = "> YES   NO " if session_data["add_time_choice"] == 1 else "  YES > NO "
            lcd_write(["   ADD MINUTES TO",
                       f"   {SLOT_NAMES[session_data['selected_slot']]}?",
                       ch, "B2:MOVE B3:OK"])
        time.sleep(0.1)

# ═══════════════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static', static_url_path='/static')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

def get_user_id():
    uid = request.cookies.get('user_uuid')
    if not uid: uid = request.remote_addr
    db = load_db()
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    return uid

@app.route('/')
def index():
    db = load_db()
    uid = request.cookies.get('user_uuid')
    is_new_user = False
    if not uid:
        uid = str(uuid.uuid4())[:8]
        is_new_user = True
    if uid not in db["users"]:
        db["users"][uid] = {"points": 0}
        save_db(db)
    resp = make_response(render_template('index.html',
                           device_id=uid,
                           points=db["users"][uid]["points"],
                           logs=db["logs"][-5:]))
    if is_new_user:
        resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

@app.route('/api/status')
def get_status():
    uid = get_user_id()
    db  = load_db()
    return jsonify({
        "state":           session_data["state"],
        "session":         session_data["count"],
        "points":          db["users"][uid]["points"],
        "slots":           [slot_status[i] for i in range(4)],
        "is_my_session":   session_data["active_user"] == uid,
        "last_bottle_msg": session_data.get("last_bottle_msg", "")
    })

@app.route('/api/start_session')
def web_start():
    uid = get_user_id()
    if session_data["state"] == "IDLE":
        session_data.update({"state": "INSERTING", "count": 0,
                              "active_user": uid, "last_activity": time.time()})
        return jsonify({"status": "ok"})
    return jsonify({"status": "busy"}), 403

@app.route('/api/stop_session')
def web_stop():
    uid = get_user_id()
    if session_data["active_user"] == uid:
        db    = load_db()
        added = session_data["count"]
        if added > 0:
            db["users"][uid]["points"] += added
            db["total_bottles"]        += added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"])
            save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
        return jsonify({"status": "ok"})
    return jsonify({"status": "unauthorized"}), 401

@app.route('/api/emergency_reset')
def admin_reset():
    system_refresh()
    return jsonify({"status": "system_refreshed"})

def system_refresh():
    for i in range(4): slot_status[i] = 0
    for pin in PINS_RELAYS: gpio_write(pin, 0)
    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
    beep(3)

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pass') != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    db        = load_db()
    user_list = [{"user_id": k, "points": v.get("points", 0)} for k, v in db["users"].items()]
    return jsonify({"total_bottles": db.get("total_bottles", 0), "users": user_list})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_user_id()
    db  = load_db()
    if db["users"][uid]["points"] >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]])
        save_db(db)
        start_or_extend_relay(slot, pts * 300)
        beep(2)
    return redirect(url_for('index'))

# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)

    # Force all outputs OFF
    gpio_setup(PIN_BUZZER, "out", "0"); gpio_write(PIN_BUZZER, 0)
    gpio_setup(PIN_SERVO,  "out", "0"); gpio_write(PIN_SERVO,  0)
    for p in PINS_RELAYS:
        gpio_setup(p, "out", "0"); gpio_write(p, 0)

    init_lcd()
    lcd_write(["  ECO-CHARGE VENDO  ", "   Initializing...  ", "                    ", "                    "])

    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
        gpio_setup(p, "in")

    # HX711 init
    lcd_write(["  ECO-CHARGE VENDO  ", "  Calibrating...    ", "  Please wait...    ", "                    "])
    hx_begin()
    hx_auto_zero()

    # Servo home at 90deg
    servo_deg = 90
    threading.Thread(target=servo_pwm_thread, daemon=True).start()
    time.sleep(0.5)

    lcd_write(["  ECO-CHARGE VENDO  ", "      READY!        ", "                    ", "                    "])
    time.sleep(1)

    threading.Thread(target=hardware_loop,   daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
