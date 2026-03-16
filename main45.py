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
hx_calibration  = 441.17
hx_ready        = False  # set True once loadcell.py is detected

# ═══════════════════════════════════════════════════════════
# SERVO — matching working servo code (PA10, dev/mem)
# ═══════════════════════════════════════════════════════════
import mmap, struct, queue

PIN_SERVO      = "10"    # PA10
PULSE_MIN_MS   = 0.5
PULSE_MAX_MS   = 2.5
PERIOD_MS      = 20.0
PA_BASE        = 0x01C20800
PA_DAT_OFF     = 0x10
PA10_BIT       = (1 << 10)
_servo_mem     = None
_servo_sysfs   = False
_servo_queue   = queue.Queue(maxsize=1)
servo_active   = True

def servo_init():
    global _servo_mem, _servo_sysfs
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        _servo_mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                               mmap.PROT_READ | mmap.PROT_WRITE,
                               offset=PA_BASE & ~0xFFF)
        os.close(fd)
        _servo_sysfs = False
        print("Servo: /dev/mem (high accuracy)")
    except Exception as e:
        _servo_sysfs = True
        print(f"Servo: sysfs fallback ({e})")
        path = f"/sys/class/gpio/gpio{PIN_SERVO}"
        if os.path.exists(path):
            try:
                with open("/sys/class/gpio/unexport", "w") as f: f.write(PIN_SERVO)
            except: pass
            time.sleep(0.2)
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(PIN_SERVO)
            time.sleep(0.2)
            with open(f"{path}/direction", "w") as f: f.write("out")
        except: pass

def servo_set_pin(val):
    if not _servo_sysfs and _servo_mem:
        off = (PA_BASE & 0xFFF) + PA_DAT_OFF
        cur = struct.unpack_from("<I", _servo_mem, off)[0]
        struct.pack_into("<I", _servo_mem, off,
                         cur | PA10_BIT if val else cur & ~PA10_BIT)
    else:
        try:
            with open(f"/sys/class/gpio/gpio{PIN_SERVO}/value", "w") as f:
                f.write("1" if val else "0")
        except: pass

def servo_send_pulse(pulse_ms):
    pulse_s  = pulse_ms / 1000.0
    period_s = PERIOD_MS / 1000.0
    t0 = time.perf_counter()
    servo_set_pin(1)
    while (time.perf_counter() - t0) < pulse_s: pass
    servo_set_pin(0)
    while (time.perf_counter() - t0) < period_s: pass

def servo_pwm_thread():
    """Continually pulses — keeps last angle until new command received."""
    pulse_ms = PULSE_MIN_MS + (90 / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    while servo_active:
        try:
            pulse_ms = _servo_queue.get_nowait()
        except queue.Empty:
            pass
        servo_send_pulse(pulse_ms)
        time.sleep(0.001)

def servo_goto(deg, hold=0.6):
    """Move servo to angle and hold for hold seconds."""
    deg      = max(0, min(180, deg))
    pulse_ms = PULSE_MIN_MS + (deg / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    try: _servo_queue.get_nowait()
    except queue.Empty: pass
    _servo_queue.put(pulse_ms)
    time.sleep(hold)

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
lcd_lock    = threading.Lock()   # prevents display_manager overlapping process_bottle

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

def _lcd_write_raw(new_lines):
    """Internal — write to LCD without acquiring lock."""
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

def lcd_write(new_lines):
    """Thread-safe LCD write — skips if lcd_lock held by process_bottle."""
    if lcd_lock.locked():
        return   # process_bottle has control, don't overlap
    with lcd_lock:
        _lcd_write_raw(new_lines)

def lcd_write_force(new_lines):
    """Force LCD write — used inside process_bottle (already holds lcd_lock)."""
    _lcd_write_raw(new_lines)

def format_time(seconds):
    seconds = max(0, seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}" if m < 60 else f"{m//60:02d}:{m%60:02d}"

# ═══════════════════════════════════════════════════════════
# HX711 — reads from loadcell.py via shared file
# ═══════════════════════════════════════════════════════════
WEIGHT_FILE = "/tmp/eco_weight.json"
CMD_FILE    = "/tmp/eco_cmd.txt"

def hx_get_grams():
    """Read latest weight from loadcell.py shared file."""
    try:
        with open(WEIGHT_FILE, "r") as f:
            data = json.load(f)
        # Stale check — ignore if older than 1 second
        if time.time() - data.get("ts", 0) > 1.0:
            return None
        return data.get("grams", 0.0)
    except:
        return None

def hx_fast_tare():
    """Send TARE command to loadcell.py."""
    try:
        with open(CMD_FILE, "w") as f:
            f.write("TARE")
        print("Tare command sent to loadcell.py")
        time.sleep(0.8)  # wait for loadcell.py to tare
    except Exception as e:
        print(f"Tare command failed: {e}")

def hx_is_ready():
    """Check if loadcell.py is running and sending data."""
    try:
        with open(WEIGHT_FILE, "r") as f:
            data = json.load(f)
        return time.time() - data.get("ts", 0) < 2.0
    except:
        return False

# ═══════════════════════════════════════════════════════════
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
        with lcd_lock:
            _process_bottle_inner()

def _process_bottle_inner():
    """Inner logic — called with lcd_lock held."""

    # Ensure servo is at standby 90deg
    servo_goto(90, hold=0.3)

    # Start sampling weight immediately
    lcd_write_force([
        "    BOTTLE SENSED   ",
        "    Weighing...     ",
        "  Place on sensor   ",
        "  Hold still...     "
    ])

    samples    = []
    start_time = time.time()
    timeout    = 5.0

    while time.time() - start_time < timeout:
        if hx_is_ready():
            w = hx_get_grams()
            if w is not None and w > WEIGHT_MIN_VALID:
                samples.append(w)

        if samples:
            avg = sum(samples) / len(samples)
            lcd_write_force([
                "    WEIGHING...     ",
                f"    {avg:6.1f} g       "[:20],
                f"  Reading {len(samples)} of 5   "[:20],
                "  Hold still...     "
            ])

        if len(samples) >= 5:
            samples_sorted = sorted(samples[-5:])
            variance = samples_sorted[-1] - samples_sorted[0]
            if variance <= 5.0:
                break

        time.sleep(0.4)

    if len(samples) < 3:
        servo_goto(90, hold=0)
        lcd_write_force([
            "   NO BOTTLE        ",
            "   DETECTED!        ",
            "  Place bottle on   ",
            "  sensor and retry  "
        ])
        beep_now(3)
        time.sleep(2)
        return

    final    = sorted(samples[-5:]) if len(samples) >= 5 else sorted(samples)
    weight   = final[len(final) // 2]
    variance = final[-1] - final[0]

    if variance > 5.0:
        servo_goto(90, hold=0)
        lcd_write_force([
            "   HOLD STILL!      ",
            "  Keep bottle firm  ",
            "  on the sensor     ",
            "  Try again...      "
        ])
        time.sleep(2)
        return

    pts, label, line1, line2 = classify_bottle(weight)
    print(f"Bottle: {weight:.1f}g pts={pts} label={label}")

    if pts > 0:
        lcd_write_force([
            "    ACCEPTED!       ",
            line1,
            line2,
            "   Processing...    "
        ])
        time.sleep(0.5)
        servo_goto(0, hold=1.0)

        session_data["count"]          += pts
        session_data["last_activity"]   = time.time()
        session_data["last_bottle_msg"] = line1.strip()
        beep_now(pts)

        # Only log bottle — points saved to user when session ends
        db = load_db()
        db["total_bottles"] += 1
        db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label or "Bottle"])
        save_db(db)

        lcd_write_force([
            "  REMOVING BOTTLE    ",
            "  Please wait...     ",
            "                    ",
            "                    "
        ])
        deadline = time.time() + 6.0
        while time.time() < deadline:
            w = hx_get_grams()
            if w is not None and w < WEIGHT_MIN_VALID:
                break
            time.sleep(0.2)

        servo_goto(90, hold=0.5)

        total = session_data["count"]
        lcd_write_force([
            line1,
            line2,
            f"  Total: {total} pts        "[:20],
            "  Keep recycling!   "
        ])
        time.sleep(2)

        # Ready for next bottle — loadcell.py handles its own tare
        cnt = session_data['count']
        lcd_write_force([
            "   INSERT BOTTLE    ",
            f"   Bottles: {cnt}        "[:20],
            f"   Time: {cnt*5:.0f}m        "[:20],
            "   B3: Confirm      "
        ])

    else:
        servo_goto(90, hold=0)
        lcd_write_force([
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
    ir_last_bot = 0   # 0=no object, 1=object detected (active HIGH)
    ir_last_top = 0

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

        # IR sensors — BOTH bottom AND top trigger bottle detection (edge detection)
        if session_data["state"] == "INSERTING" and now >= ir_cooldown \
                and not admin_data["active"] and not bottle_lock.locked():
            bot = gpio_read(PIN_IR_BOTTOM)
            top = gpio_read(PIN_IR_TOP)

            # Rising edge (LOW→HIGH = object detected, active HIGH sensors)
            bot_triggered = (bot == 1 and ir_last_bot == 0)
            top_triggered = (top == 1 and ir_last_top == 0)

            if bot_triggered or top_triggered:
                print(f"IR triggered: bot={bot} top={top} bot_edge={bot_triggered} top_edge={top_triggered}")
                ir_cooldown = now + 6.0
                threading.Thread(
                    target=process_bottle,
                    daemon=True
                ).start()

            ir_last_bot = bot
            ir_last_top = top

        # Auto timeout 30s — pauses if bottle is on sensor
        if not admin_data["active"]:
            if session_data["state"] == "INSERTING":
                # Reset timer if bottle is on load cell
                weight_now = hx_get_grams()
                if weight_now is not None and weight_now > 5.0:
                    session_data["last_activity"] = now  # bottle present — keep alive
                elif (now - session_data["last_activity"]) > 30:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
                    lcd_write(["   SESSION ENDED    ",
                               "  No activity 30s   ",
                               "   Press START to   ",
                               "   insert bottles   "])
            elif session_data["state"] not in ["IDLE", "THANK_YOU"]:
                if (now - session_data["last_activity"]) > 30:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
        else:
            session_data["last_activity"] = now

        time.sleep(0.01)

# ═══════════════════════════════════════════════════════════
# DISPLAY MANAGER
# ═══════════════════════════════════════════════════════════
def display_manager():
    last_display = []
    while True:
        # Skip if process_bottle controls LCD
        if lcd_lock.locked():
            time.sleep(0.05)
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
                lines = ["  ** ADMIN MODE **  ", f"ID:{uid[:12]}", f"PTS:{pts}  {idx+1}/{len(users)}", "B1:RST B2:NXT B3:EXIT"]
            else:
                lines = ["  ** ADMIN MODE **  ", f" TOTAL:{total} bottles", "   No users yet", "B1:RST       B3:EXIT"]
        elif s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lines = ["      ECO VENDO     ", "     PRESS START    ", t1, t2]
        elif s == "INSERTING":
            cnt = session_data['count']
            lines = ["   INSERT BOTTLE    ",
                     f"   BOTTLES: {cnt}        "[:20],
                     f"   TIME: {cnt*5:.0f}m         "[:20],
                     "   B3:CONFIRM       "]
        elif s == "SELECTING":
            lines = ["      SELECT        ",
                     f"  > {SLOT_NAMES[session_data['selected_slot']]}         "[:20],
                     f"  FOR {session_data['count']*5:.0f} MINS      "[:20],
                     "   B3:CONFIRM       "]
        elif s == "ADD_TIME_PROMPT":
            ch = "> YES   NO          " if session_data["add_time_choice"] == 1 else "  YES > NO          "
            lines = ["   ADD MINUTES TO   ",
                     f"   {SLOT_NAMES[session_data['selected_slot']]}?          "[:20],
                     ch, "  B2:MOVE  B3:OK    "]
        else:
            time.sleep(0.05)
            continue

        # Only write if content changed
        if lines != last_display:
            lcd_write(lines)
            last_display = lines[:]

        time.sleep(0.05)  # 20fps instead of 10fps

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

    # HX711 — init GPIO only, no auto-zero at startup
    time.sleep(0.5)  # let HX711 stabilize after GPIO init

    # Start HX711 background reading thread

    # Servo standby at 90deg
    # Servo — init and start at 90deg standby
    servo_init()
    threading.Thread(target=servo_pwm_thread, daemon=True).start()
    servo_goto(90, hold=0.5)  # move to standby position
    time.sleep(0.5)

    lcd_write(["  ECO-CHARGE VENDO  ", "    PRESS START     ", "                    ", "                    "])

    threading.Thread(target=hardware_loop,   daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
