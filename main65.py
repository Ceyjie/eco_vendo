import time, threading, os, subprocess, json, uuid, signal
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory
from RPLCD.i2c import CharLCD
import gpiod
from gpiod.line import Direction, Bias
import mmap, struct, queue

# ═══════════════════════════════════════════════════════════
# PIN CONFIG
# ═══════════════════════════════════════════════════════════
PIN_IR_BOTTOM_OFF = 6    
PIN_IR_TOP_OFF    = 1    
CHIP_PATH         = "/dev/gpiochip0"

PIN_IR_BOTTOM  = "6"
PIN_IR_TOP     = "1"
PIN_BUZZER     = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS    = ["3", "2", "67", "21"]
PIN_SERVO      = "10"
SLOT_NAMES     = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE        = "eco_database.json"
ADMIN_PASSWORD = "1234"

# BOTTLE WEIGHT RANGES
WEIGHT_MIN_VALID  =  8
WEIGHT_SMALL_MIN  =  9
WEIGHT_SMALL_MAX  = 16
WEIGHT_NATURE_MIN = 16
WEIGHT_NATURE_MAX = 19
WEIGHT_BIG_MIN    = 29
WEIGHT_BIG_MAX    = 39

# ═══════════════════════════════════════════════════════════
# SERVO CONTROL
# ═══════════════════════════════════════════════════════════
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
    pulse_ms    = PULSE_MIN_MS + (90 / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    pulse_count = 0
    MAX_PULSES  = 100  # Increased to 2 seconds of holding force

    while servo_active:
        try:
            pulse_ms    = _servo_queue.get_nowait()
            pulse_count = 0
        except queue.Empty:
            pass

        if pulse_count < MAX_PULSES:
            servo_send_pulse(pulse_ms)
            pulse_count += 1
        else:
            servo_set_pin(0)
            time.sleep(0.02)

def servo_goto(deg, hold=1.6):
    deg      = max(0, min(180, deg))
    pulse_ms = PULSE_MIN_MS + (deg / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    while not _servo_queue.empty():
        try: _servo_queue.get_nowait()
        except: break
    _servo_queue.put(pulse_ms)
    if hold > 0:
        time.sleep(hold)

# ═══════════════════════════════════════════════════════════
# SYSTEM STATE & DB
# ═══════════════════════════════════════════════════════════
session_data = {"state": "IDLE", "count": 0, "active_user": None, "selected_slot": 0, "add_time_choice": 1, "last_activity": time.time(), "last_bottle_msg": ""}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
admin_data   = {"active": False, "user_index": 0, "hold_start": 0, "holding": False}
bottle_lock  = threading.Lock()
lcd_lock     = threading.Lock()
_ir_reset    = False

def load_db():
    if not os.path.exists(DB_FILE): return {"total_bottles": 0, "users": {}, "logs": []}
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

# ═══════════════════════════════════════════════════════════
# GPIO & HELPERS
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

def beep_now(times=1):
    for _ in range(times):
        gpio_write(PIN_BUZZER, 1); time.sleep(0.08)
        gpio_write(PIN_BUZZER, 0); time.sleep(0.04)

def beep(times=1):
    threading.Thread(target=lambda: beep_now(times), daemon=True).start()

# ═══════════════════════════════════════════════════════════
# IR SENSORS (GPIOD)
# ═══════════════════════════════════════════════════════════
_ir_request = None
def ir_init():
    global _ir_request
    for pin in [PIN_IR_BOTTOM, PIN_IR_TOP]:
        if os.path.exists(f"/sys/class/gpio/gpio{pin}"):
            try:
                with open("/sys/class/gpio/unexport", "w") as f: f.write(pin)
            except: pass
    time.sleep(0.3)
    try:
        _ir_request = gpiod.request_lines(CHIP_PATH, consumer="eco-ir",
            config={(PIN_IR_BOTTOM_OFF, PIN_IR_TOP_OFF): gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)})
    except:
        _ir_request = None
        for pin in [PIN_IR_BOTTOM, PIN_IR_TOP]: gpio_setup(pin, "in")

def ir_read_both():
    if _ir_request:
        try:
            vals = _ir_request.get_values()
            return vals[0].value, vals[1].value
        except: pass
    return gpio_read(PIN_IR_BOTTOM), gpio_read(PIN_IR_TOP)

# ═══════════════════════════════════════════════════════════
# LCD CONTROL
# ═══════════════════════════════════════════════════════════
lcd = None
current_lcd_lines = ["", "", "", ""]

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear(); return
        except: lcd = None

def _lcd_write_raw(new_lines):
    global current_lcd_lines, lcd
    if not lcd: init_lcd(); return
    try:
        safe_lines = [str(line).ljust(20)[:20] for line in new_lines]
        for i, line in enumerate(safe_lines):
            if line != current_lcd_lines[i]:
                lcd.cursor_pos = (i, 0); lcd.write_string(line); current_lcd_lines[i] = line
    except:
        lcd = None; init_lcd()

def lcd_write(new_lines):
    if not lcd_lock.locked():
        with lcd_lock: _lcd_write_raw(new_lines)

def lcd_write_force(new_lines):
    _lcd_write_raw(new_lines)

def format_time(seconds):
    m, s = divmod(max(0, seconds), 60)
    return f"{m:02d}:{s:02d}"

# ═══════════════════════════════════════════════════════════
# HX711 BRIDGE
# ═══════════════════════════════════════════════════════════
WEIGHT_FILE = "/tmp/eco_weight.json"
def hx_get_grams():
    try:
        with open(WEIGHT_FILE, "r") as f:
            data = json.load(f)
            if time.time() - data.get("ts", 0) > 1.0: return None
            return data.get("grams", 0.0)
    except: return None

def hx_is_ready():
    try:
        with open(WEIGHT_FILE, "r") as f:
            return time.time() - json.load(f).get("ts", 0) < 2.0
    except: return False

# ═══════════════════════════════════════════════════════════
# BOTTLE LOGIC
# ═══════════════════════════════════════════════════════════
def classify_bottle(weight_g):
    if weight_g < WEIGHT_MIN_VALID: return 0, None, "   NOT A BOTTLE!    ", "   Bottles only!    "
    if WEIGHT_SMALL_MIN <= weight_g <= WEIGHT_SMALL_MAX: return 1, "SMALL", "  SMALL BOTTLE      ", "      +1 POINT!      "
    if WEIGHT_NATURE_MIN <= weight_g <= WEIGHT_NATURE_MAX: return 2, "NATURE", "  NATURE SPRING     ", "      +2 POINTS!     "
    if WEIGHT_BIG_MIN <= weight_g <= WEIGHT_BIG_MAX: return 2, "BIG", "  BIG BOTTLE        ", "      +2 POINTS!     "
    return 0, None, "  INVALID BOTTLE!   ", f"  {weight_g:.0f}g not accepted "[:20]

def process_bottle():
    with bottle_lock:
        with lcd_lock: _process_bottle_inner()

def _process_bottle_inner():
    global _ir_reset
    servo_goto(90, hold=0.5)
    lcd_write_force(["    BOTTLE SENSED   ", "    Checking...     ", "  Place on sensor   ", "  Hold still...     "])
    
    samples = []
    start_time = time.time()
    while time.time() - start_time < 5.0:
        w = hx_get_grams()
        if w is not None and w > WEIGHT_MIN_VALID:
            samples.append(w)
            avg = sum(samples) / len(samples)
            lcd_write_force(["    WEIGHING...     ", f"    {avg:6.1f} g"[:20], f"  Reading {len(samples)} of 5", "  Hold still...     "])
        if len(samples) >= 5:
            s = sorted(samples[-5:])
            if s[-1] - s[0] <= 5.0: break
        time.sleep(0.4)

    if len(samples) < 3:
        lcd_write_force(["    NO BOTTLE       ", "    DETECTED!       ", "  Place bottle on   ", "  sensor and retry  "])
        beep_now(3); time.sleep(2); _ir_reset = True; return

    final = sorted(samples[-5:])
    weight = final[len(final) // 2]
    pts, label, line1, line2 = classify_bottle(weight)

    if pts > 0:
        lcd_write_force(["    ACCEPTED!       ", line1, line2, "    Processing...    "])
        time.sleep(0.5)
        
        # 1. Open Hatch
        servo_goto(0, hold=1.6)

        # Update Session
        session_data["count"] += pts
        session_data["last_activity"] = time.time()
        session_data["last_bottle_msg"] = line1.strip()
        beep_now(pts)

        # Update DB
        db = load_db()
        db["total_bottles"] += 1
        db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label or "Bottle"])
        save_db(db)

        # 2. FIXED: Guaranteed Return Logic
        lcd_write_force(["  DROPPING BOTTLE   ", "  Please wait...    ", "", ""])
        time.sleep(2.0)  # Time for bottle to fall
        servo_goto(90, hold=1.6) # Force Return

        lcd_write_force([line1, line2, f"  Total: {session_data['count']} pts", "  Keep recycling!   "])
        time.sleep(2)
    else:
        servo_goto(90, hold=1.6)
        lcd_write_force(["    INVALID ITEM!   ", line1, line2, "    Try again...     "])
        beep_now(3); time.sleep(2)

    _ir_reset = True

# ═══════════════════════════════════════════════════════════
# RELAYS, BUTTONS, & HARDWARE LOOP
# ═══════════════════════════════════════════════════════════
def run_relay_thread(slot):
    gpio_write(PINS_RELAYS[slot], 1)
    while slot_status[slot] > 0:
        time.sleep(1); slot_status[slot] = max(0, slot_status[slot] - 1)
    gpio_write(PINS_RELAYS[slot], 0)

def start_or_extend_relay(slot, sec):
    is_run = slot_status[slot] > 0
    slot_status[slot] += sec
    if not is_run: threading.Thread(target=run_relay_thread, args=(slot,), daemon=True).start()

def handle_physical_press(pin):
    global _ir_reset
    session_data["last_activity"] = time.time()
    s = session_data["state"]
    if admin_data["active"]:
        if pin == PIN_BTN_SELECT:
            u = list(load_db()["users"].items())
            if u: admin_data["user_index"] = (admin_data["user_index"] + 1) % len(u)
            beep(1)
        elif pin == PIN_BTN_START: system_refresh()
        elif pin == PIN_BTN_CONFIRM: admin_data["active"] = False; session_data.update({"state": "IDLE", "count": 0}); beep(2)
        return

    if pin == PIN_BTN_START and s == "IDLE": beep(1); _ir_reset = True; session_data.update({"state": "INSERTING", "count": 0, "active_user": "LOCAL_USER"})
    elif pin == PIN_BTN_SELECT:
        if s == "SELECTING": beep(1); session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        elif s == "ADD_TIME_PROMPT": beep(1); session_data["add_time_choice"] = 1 - session_data["add_time_choice"]
    elif pin == PIN_BTN_CONFIRM:
        if s == "INSERTING": beep(1); session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        elif s == "SELECTING":
            beep(1)
            if slot_status[session_data["selected_slot"]] > 0: session_data["state"] = "ADD_TIME_PROMPT"
            else: finalize_transaction()
        elif s == "ADD_TIME_PROMPT":
            if session_data["add_time_choice"] == 1: finalize_transaction()
            else: beep(1); session_data["state"] = "SELECTING"

def finalize_transaction():
    start_or_extend_relay(session_data["selected_slot"], session_data["count"] * 300)
    session_data["state"] = "THANK_YOU"; beep(2)
    lcd_write(["      THANK YOU!    ", " You helped protect ", " our environment by ", " recycling plastic! "])
    threading.Timer(4.0, lambda: session_data.update({"state": "IDLE", "count": 0})).start()

def hardware_loop():
    global _ir_reset
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}; last_press = {p: 0.0 for p in btn_pins}
    ir_cooldown, ir_bot_cnt, ir_top_cnt = 0, 0, 0
    while True:
        now = time.time()
        for p in btn_pins:
            v = gpio_read(p)
            if v == 0 and last_val[p] == 1 and (now - last_press[p]) > 0.3:
                last_press[p] = now; handle_physical_press(p)
            last_val[p] = v

        if _ir_reset: ir_bot_cnt = ir_top_cnt = 0; ir_cooldown = now + 1.0; _ir_reset = False

        if session_data["state"] == "INSERTING" and now >= ir_cooldown and not bottle_lock.locked():
            bot, top = ir_read_both()
            ir_bot_cnt = (ir_bot_cnt + 1) if bot == 0 else 0
            ir_top_cnt = (ir_top_cnt + 1) if top == 0 else 0
            if ir_bot_cnt >= 3 or ir_top_cnt >= 3:
                ir_cooldown = now + 6.0; beep_now(1)
                threading.Thread(target=process_bottle, daemon=True).start()
        
        if session_data["state"] != "IDLE" and (now - session_data["last_activity"]) > 30:
            session_data.update({"state": "IDLE", "count": 0}); beep(3)
        time.sleep(0.01)

# ═══════════════════════════════════════════════════════════
# DISPLAY & FLASK
# ═══════════════════════════════════════════════════════════
def display_manager():
    last_lines = []
    while True:
        if not lcd_lock.locked():
            s = session_data["state"]
            if admin_data["active"]:
                db = load_db(); u = list(db["users"].items())
                if u:
                    idx = admin_data["user_index"] % len(u); uid, ud = u[idx]
                    lines = ["  ** ADMIN MODE ** ", f"ID:{uid[:16]}", f"PTS:{ud.get('points',0)} {idx+1}/{len(u)}", "B1:RST B2:NXT B3:EXIT"]
                else: lines = ["  ** ADMIN MODE ** ", f" TOTAL:{db.get('total_bottles',0)}", "    No users yet", "B1:RST       B3:EXIT"]
            elif s == "IDLE":
                t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
                t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
                lines = ["      ECO VENDO     ", "     PRESS START    ", t1, t2]
            elif s == "INSERTING":
                c = session_data['count']
                lines = ["   INSERT BOTTLE    ", f"   BOTTLES: {c}", f"   TIME: {c*5}m", "   B3:CONFIRM       "]
            elif s == "SELECTING":
                lines = ["      SELECT        ", f"  > {SLOT_NAMES[session_data['selected_slot']]}", f"  FOR {session_data['count']*5} MINS", "   B3:CONFIRM       "]
            elif s == "ADD_TIME_PROMPT":
                ch = "> YES   NO          " if session_data["add_time_choice"] == 1 else "  YES > NO          "
                lines = ["   ADD MINUTES TO   ", f"   {SLOT_NAMES[session_data['selected_slot']]}?", ch, "  B2:MOVE  B3:OK    "]
            else: time.sleep(0.1); continue

            if lines != last_lines: lcd_write(lines); last_lines = lines[:]
        time.sleep(0.1)

app = Flask(__name__)
@app.route('/')
def index():
    db = load_db(); uid = request.cookies.get('user_uuid') or str(uuid.uuid4())[:8]
    if uid not in db["users"]: db["users"][uid] = {"points": 0}; save_db(db)
    resp = make_response(render_template('index.html', device_id=uid, points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    resp.set_cookie('user_uuid', uid, max_age=31536000); return resp

def system_refresh():
    for i in range(4): slot_status[i] = 0
    for p in PINS_RELAYS: gpio_write(p, 0)
    session_data.update({"state": "IDLE", "count": 0}); beep(3)

def shutdown(sig, frame):
    global servo_active
    servo_active = False; [[gpio_write(p, 0) for p in PINS_RELAYS], gpio_write(PIN_BUZZER, 0), gpio_write(PIN_SERVO, 0)]
    if _ir_request: _ir_request.release()
    os._exit(0)

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    signal.signal(signal.SIGINT, shutdown)
    [gpio_setup(p, "out", "0") for p in PINS_RELAYS + [PIN_BUZZER, PIN_SERVO]]
    [gpio_setup(p, "in") for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]]
    init_lcd(); ir_init(); servo_init()
    threading.Thread(target=servo_pwm_thread, daemon=True).start()
    servo_goto(90, hold=1.0)
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
