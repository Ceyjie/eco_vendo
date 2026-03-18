import time, threading, os, subprocess, json, uuid, signal
import mmap, struct, queue
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, send_from_directory
from RPLCD.i2c import CharLCD

# ═══════════════════════════════════════════════════════════
# PIN CONFIG
# ═══════════════════════════════════════════════════════════
PIN_IR_BOTTOM, PIN_IR_TOP              = "6", "1"
PIN_BUZZER                             = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS                            = ["3", "2", "67", "21"]
PIN_SERVO                              = "10"
SLOT_NAMES     = ["USB 1", "USB 2", "USB 3", "AC 220V"]
DB_FILE        = "eco_database.json"
ADMIN_PASSWORD = "1234"

# ═══════════════════════════════════════════════════════════
# BOTTLE WEIGHT RANGES
# ═══════════════════════════════════════════════════════════
WEIGHT_MIN_VALID  =  8
WEIGHT_SMALL_MIN  =  9
WEIGHT_SMALL_MAX  = 16
WEIGHT_NATURE_MIN = 16
WEIGHT_NATURE_MAX = 19
WEIGHT_COKE1L_MIN = 22
WEIGHT_COKE1L_MAX = 28
WEIGHT_BIG_MIN    = 29
WEIGHT_BIG_MAX    = 39

# ═══════════════════════════════════════════════════════════
# SERVO
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
    pulse_ms = PULSE_MIN_MS + (90 / 180.0) * (PULSE_MAX_MS - PULSE_MIN_MS)
    while servo_active:
        try:
            pulse_ms = _servo_queue.get_nowait()
        except queue.Empty:
            pass
        servo_send_pulse(pulse_ms)
        time.sleep(0.001)

def servo_goto(deg, hold=0.6):
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
    "last_bottle_msg": "",
    "bottle_result": "error",
    "bottle_result_choice": 0
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}
admin_data  = {"active": False, "user_index": 0, "hold_start": 0, "holding": False}
bottle_lock = threading.Lock()
lcd_lock    = threading.Lock()

def is_web_session():
    """Returns True if the current session was started from the web UI."""
    u = session_data.get("active_user")
    return u is not None and u != "LOCAL_USER"

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
# GPIO — Allwinner H3 pull-up support (Orange Pi One)
# ═══════════════════════════════════════════════════════════
H3_PULL_MAP = {
    # sysfs_pin : (PUL_register_address, bit_shift)
    "0":  (0x01C2081C,  0),   # PA0
    "1":  (0x01C2081C,  2),   # PA1
    "2":  (0x01C2081C,  4),   # PA2
    "3":  (0x01C2081C,  6),   # PA3
    "6":  (0x01C2081C, 12),   # PA6
    "10": (0x01C2081C, 20),   # PA10
    "13": (0x01C2081C, 26),   # PA13
    "14": (0x01C2081C, 28),   # PA14
    "21": (0x01C20820, 10),   # PA21 → PUL1
    "67": (0x01C20864,  6),   # PC3
    "110":(0x01C20888, 28),   # PD14
}

def _gpio_set_pull(pin, mode="up"):
    if pin not in H3_PULL_MAP:
        print(f"Pull: no H3 map for pin {pin}, skipped")
        return
    reg_addr, bit_shift = H3_PULL_MAP[pin]
    val = 0b01 if mode == "up" else (0b10 if mode == "down" else 0b00)
    try:
        fd  = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                        mmap.PROT_READ | mmap.PROT_WRITE,
                        offset=reg_addr & ~0xFFF)
        os.close(fd)
        off = reg_addr & 0xFFF
        cur = struct.unpack_from("<I", mem, off)[0]
        cur = (cur & ~(0b11 << bit_shift)) | (val << bit_shift)
        struct.pack_into("<I", mem, off, cur)
        mem.close()
        print(f"  pull-{mode} OK  pin={pin}  reg=0x{reg_addr:08X}  bit={bit_shift}")
    except Exception as e:
        print(f"  pull FAILED pin={pin}: {e}")

def gpio_setup(pin, direction="in", value="0", pull=None):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"{path}/value", "w") as f: f.write(value)
    if pull and direction == "in":
        _gpio_set_pull(pin, pull)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except:
        return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
            f.write(str(val))
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
# LCD  — fast direct writer, no busy-wait on lock
# ═══════════════════════════════════════════════════════════
lcd = None
current_lcd_lines = ["", "", "", ""]

# Pending lines that display_manager wants to show.
# _lcd_pending is written by display_manager and consumed by the
# dedicated lcd_writer_thread — this decouples display logic from
# the I2C write latency completely.
_lcd_pending      = None
_lcd_pending_lock = threading.Lock()

def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear()
            print(f"LCD found at 0x{addr:02X}")
            return
        except:
            lcd = None

def _lcd_write_raw(new_lines):
    """Write lines directly to LCD, skipping unchanged lines."""
    global current_lcd_lines, lcd
    if not lcd:
        init_lcd()
        if not lcd:
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
        time.sleep(0.5)
        init_lcd()

def lcd_writer_thread():
    """
    Dedicated thread that owns the I2C bus.
    display_manager posts desired lines to _lcd_pending;
    this thread picks them up and writes immediately.
    lcd_write_force bypasses the queue and writes directly
    (called only from bottle_lock context where display_manager
    is already blocked by lcd_lock).
    """
    global _lcd_pending
    while True:
        lines = None
        with _lcd_pending_lock:
            if _lcd_pending is not None:
                lines = _lcd_pending
                _lcd_pending = None
        if lines is not None:
            with lcd_lock:
                _lcd_write_raw(lines)
        time.sleep(0.01)   # 10 ms — fast enough, doesn't hammer I2C

def lcd_write(new_lines):
    """Non-blocking post to lcd_writer_thread."""
    global _lcd_pending
    with _lcd_pending_lock:
        _lcd_pending = new_lines   # overwrite — always show the latest

def lcd_write_force(new_lines):
    """
    Direct synchronous write used inside bottle processing
    (already holds lcd_lock via process_bottle).
    Also clears any pending post so the writer thread
    doesn't overwrite us right after.
    """
    global _lcd_pending
    with _lcd_pending_lock:
        _lcd_pending = None
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
    try:
        with open(WEIGHT_FILE, "r") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) > 1.0:
            return None
        return data.get("grams", 0.0)
    except:
        return None

def hx_is_ready():
    try:
        with open(WEIGHT_FILE, "r") as f:
            data = json.load(f)
        return time.time() - data.get("ts", 0) < 2.0
    except:
        return False

# ═══════════════════════════════════════════════════════════
# BOTTLE CLASSIFICATION
# ═══════════════════════════════════════════════════════════
def classify_bottle(weight_g):
    if weight_g < WEIGHT_MIN_VALID:
        return 0, None, "   NOT A BOTTLE!    ", "   Bottles only!    "
    elif WEIGHT_SMALL_MIN <= weight_g <= WEIGHT_SMALL_MAX:
        return 1, "SMALL",  "  SMALL BOTTLE      ", "     +1 POINT!      "
    elif WEIGHT_NATURE_MIN <= weight_g <= WEIGHT_NATURE_MAX:
        return 2, "NATURE", "  NATURE SPRING     ", "     +2 POINTS!     "
    elif WEIGHT_COKE1L_MIN <= weight_g <= WEIGHT_COKE1L_MAX:
        return 2, "COKE1L", "  COKE/ROYAL 1L     ", "     +2 POINTS!     "
    elif WEIGHT_BIG_MIN <= weight_g <= WEIGHT_BIG_MAX:
        return 2, "BIG",    "  BIG BOTTLE        ", "     +2 POINTS!     "
    else:
        return 0, None,     "  INVALID BOTTLE!   ", f"  {weight_g:.0f}g not accepted  "[:20]

# ═══════════════════════════════════════════════════════════
# BOTTLE RESULT HELPER
# ═══════════════════════════════════════════════════════════
def set_bottle_result(result_type):
    session_data["state"]                = "BOTTLE_RESULT"
    session_data["bottle_result"]        = result_type
    session_data["bottle_result_choice"] = 0

# ═══════════════════════════════════════════════════════════
# BOTTLE PROCESSING
# ═══════════════════════════════════════════════════════════
def process_bottle():
    with bottle_lock:
        with lcd_lock:
            _process_bottle_inner()

def _process_bottle_inner():
    servo_goto(90, hold=0.3)

    lcd_write_force(["    BOTTLE SENSED   ", "    Checking...     ",
                     "  Place on sensor   ", "  Hold still...     "])
    time.sleep(0.5)

    pre_check = False
    for _ in range(10):
        w = hx_get_grams()
        if w is not None and w > WEIGHT_MIN_VALID:
            pre_check = True
            break
        time.sleep(0.2)

    if not pre_check:
        lcd_write_force(["   NO BOTTLE        ", "   DETECTED!        ",
                         "  Place bottle on   ", "  sensor and retry  "])
        beep_now(3)
        time.sleep(2)
        set_bottle_result("error")
        return

    samples    = []
    start_time = time.time()

    while time.time() - start_time < 5.0:
        if hx_is_ready():
            w = hx_get_grams()
            if w is not None and w > WEIGHT_MIN_VALID:
                samples.append(w)
        if samples:
            avg = sum(samples) / len(samples)
            lcd_write_force(["    WEIGHING...     ",
                             f"    {avg:6.1f} g       "[:20],
                             f"  Reading {len(samples)} of 5   "[:20],
                             "  Hold still...     "])
        if len(samples) >= 5:
            s = sorted(samples[-5:])
            if s[-1] - s[0] <= 5.0:
                break
        time.sleep(0.4)

    if len(samples) < 3:
        servo_goto(90, hold=0)
        lcd_write_force(["   NO BOTTLE        ", "   DETECTED!        ",
                         "  Place bottle on   ", "  sensor and retry  "])
        beep_now(3)
        time.sleep(2)
        set_bottle_result("error")
        return

    final    = sorted(samples[-5:]) if len(samples) >= 5 else sorted(samples)
    weight   = final[len(final) // 2]
    variance = final[-1] - final[0]

    if variance > 5.0:
        servo_goto(90, hold=0)
        lcd_write_force(["   HOLD STILL!      ", "  Keep bottle firm  ",
                         "  on the sensor     ", "  Try again...      "])
        time.sleep(2)
        set_bottle_result("error")
        return

    pts, label, line1, line2 = classify_bottle(weight)
    print(f"Bottle: {weight:.1f}g pts={pts} label={label}")

    if pts > 0:
        lcd_write_force(["    ACCEPTED!       ", line1, line2, "   Processing...    "])
        time.sleep(0.5)
        servo_goto(0, hold=1.0)

        session_data["count"]          += pts
        session_data["last_activity"]   = time.time()
        session_data["last_bottle_msg"] = line1.strip()
        beep_now(pts)

        db = load_db()
        db["total_bottles"] += 1
        db["logs"].append([time.strftime("%H:%M"), f"+{pts} Pts", label or "Bottle"])
        save_db(db)

        lcd_write_force(["  REMOVING BOTTLE   ", "  Please wait...    ",
                         "                    ", "                    "])
        deadline = time.time() + 6.0
        while time.time() < deadline:
            w = hx_get_grams()
            if w is not None and w < WEIGHT_MIN_VALID:
                break
            time.sleep(0.2)

        servo_goto(90, hold=0.5)

        total = session_data["count"]
        lcd_write_force([line1, line2,
                         f"  Total: {total} pts        "[:20],
                         "  Keep recycling!   "])
        time.sleep(2)
        set_bottle_result("accepted")

    else:
        servo_goto(90, hold=0)
        lcd_write_force(["   INVALID ITEM!    ", line1, line2, "   Try again...     "])
        beep_now(3)
        time.sleep(2)
        set_bottle_result("rejected")

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
        if lcd: lcd.clear(); lcd.backlight_enabled = False; lcd.close()
    except: pass
    os._exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ═══════════════════════════════════════════════════════════
# BUTTON HANDLERS
# ═══════════════════════════════════════════════════════════
def handle_physical_press(pin):
    session_data["last_activity"] = time.time()
    s = session_data["state"]

    # ── Admin mode overrides everything ────────────────────
    if admin_data["active"]:
        if pin == PIN_BTN_SELECT:
            db = load_db()
            users = list(db["users"].items())
            if users: admin_data["user_index"] = (admin_data["user_index"] + 1) % len(users)
            beep(1)
        elif pin == PIN_BTN_START:
            system_refresh()
        elif pin == PIN_BTN_CONFIRM:
            admin_data["active"] = False
            session_data.update({"state": "IDLE", "count": 0, "active_user": None})
            beep(2)
        return

    # ── Web session: buttons are READ-ONLY ─────────────────
    # Physical buttons do nothing when a web user owns the session.
    # The LCD still shows current state (insert bottle / points).
    if is_web_session():
        return

    # ── Physical session buttons ───────────────────────────
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
        elif s == "BOTTLE_RESULT":
            beep(1)
            session_data["bottle_result_choice"] = 1 - session_data["bottle_result_choice"]

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
        elif s == "BOTTLE_RESULT":
            beep(1)
            choice = session_data.get("bottle_result_choice", 0)
            if choice == 0:
                session_data["state"] = "INSERTING"
            else:
                if session_data["count"] > 0:
                    session_data["state"] = "SELECTING"
                else:
                    session_data.update({"state": "IDLE", "active_user": None})

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
    ir_last_top = 1

    while True:
        now = time.time()
        start_held  = gpio_read(PIN_BTN_START)  == 0
        select_held = gpio_read(PIN_BTN_SELECT) == 0
        both_held   = start_held and select_held

        if both_held:
            if not admin_data["holding"]:
                admin_data["holding"] = True; admin_data["hold_start"] = now
            elif (now - admin_data["hold_start"]) >= 10.0 and not admin_data["active"]:
                admin_data["active"] = True; admin_data["user_index"] = 0
                admin_data["holding"] = False
                for p in btn_pins: last_val[p] = 0; last_press[p] = now
                beep(3)
        else:
            admin_data["holding"] = False

        if not both_held:
            for p in btn_pins:
                val = gpio_read(p)
                if val == 0 and last_val[p] == 1:
                    if (now - last_press[p]) > 0.3:
                        last_press[p] = now; handle_physical_press(p)
                last_val[p] = val
        else:
            for p in btn_pins: last_val[p] = 0; last_press[p] = now

        # ── IR sensors: Active LOW, falling edge (1→0) ──────
        bot = gpio_read(PIN_IR_BOTTOM)
        top = gpio_read(PIN_IR_TOP)

        if session_data["state"] == "INSERTING" and now >= ir_cooldown \
                and not admin_data["active"] and not bottle_lock.locked():
            if (bot == 0 and ir_last_bot == 1) or (top == 0 and ir_last_top == 1):
                session_data["last_activity"] = now
                ir_cooldown = now + 6.0
                beep_now(1)
                threading.Thread(target=process_bottle, daemon=True).start()

        ir_last_bot = bot
        ir_last_top = top

        # ── Auto timeout 30s ─────────────────────────────────
        if not admin_data["active"]:
            if session_data["state"] == "INSERTING":
                w = hx_get_grams()
                if w is not None and w > 5.0:
                    session_data["last_activity"] = now
                elif (now - session_data["last_activity"]) > 30:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
                    lcd_write(["   SESSION ENDED    ", "  No activity 30s   ",
                               "   Press START to   ", "   insert bottles   "])
            elif session_data["state"] == "BOTTLE_RESULT":
                if (now - session_data["last_activity"]) > 30:
                    session_data.update({"state": "IDLE", "count": 0, "active_user": None})
                    beep(3)
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
    """
    Builds the desired LCD lines and posts them via lcd_write().
    Does NOT do any I2C itself — lcd_writer_thread owns the bus.
    No sleep on lcd_lock — just posts and moves on immediately.
    """
    last_lines = []

    while True:
        s = session_data["state"]

        if admin_data["active"]:
            db = load_db()
            users = list(db["users"].items())
            total = db.get("total_bottles", 0)
            if users:
                idx = admin_data["user_index"] % len(users)
                uid, udata = users[idx]
                pts = udata.get("points", 0)
                lines = ["  ** ADMIN MODE **  ",
                         f"ID:{uid[:16]}",
                         f"PTS:{pts}  {idx+1}/{len(users)}",
                         "B1:RST B2:NXT B3:EXIT"]
            else:
                lines = ["  ** ADMIN MODE **  ",
                         f" TOTAL:{total} bottles",
                         "   No users yet",
                         "B1:RST       B3:EXIT"]

        elif s == "IDLE":
            t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
            t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
            lines = ["      RAYCHARGE     ", "     PRESS START    ", t1, t2]

        elif s == "INSERTING":
            cnt = session_data['count']
            # Show web indicator if session started from web
            src = " [WEB]" if is_web_session() else "      "
            lines = [f"  INSERT BOTTLE{src}"[:20],
                     f"   BOTTLES: {cnt}        "[:20],
                     f"   TIME: {cnt*5}m          "[:20],
                     "  IR:INSERT  B3:DONE"]

        elif s == "BOTTLE_RESULT":
            result = session_data.get("bottle_result", "error")
            choice = session_data.get("bottle_result_choice", 0)
            cnt    = session_data["count"]
            if result == "accepted":
                header = f"  Total: {cnt} pts        "[:20]
            elif result == "rejected":
                header = "  INVALID BOTTLE!   "
            else:
                header = "  NO BOTTLE DET.!   "
            toggle = ">INSERT    NO(DONE) " if choice == 0 else " INSERT   >NO(DONE) "
            lines = [header, "  Insert another?   ", toggle, " B2:TOGGLE  B3:OK   "]

        elif s == "SELECTING":
            lines = ["      SELECT        ",
                     f"  > {SLOT_NAMES[session_data['selected_slot']]}           "[:20],
                     f"  FOR {session_data['count']*5} MINS        "[:20],
                     "   B3:CONFIRM       "]

        elif s == "ADD_TIME_PROMPT":
            ch = "> YES   NO          " if session_data["add_time_choice"] == 1 else "  YES > NO          "
            lines = ["   ADD MINUTES TO   ",
                     f"   {SLOT_NAMES[session_data['selected_slot']]}?           "[:20],
                     ch,
                     "  B2:MOVE  B3:OK    "]

        else:
            time.sleep(0.02)
            continue

        if lines != last_lines:
            lcd_write(lines)
            last_lines = lines[:]

        time.sleep(0.02)   # 20 ms — display_manager just builds strings, very cheap

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
        db["users"][uid] = {"points": 0}; save_db(db)
    return uid

@app.route('/')
def index():
    db = load_db(); uid = request.cookies.get('user_uuid'); is_new_user = False
    if not uid: uid = str(uuid.uuid4())[:8]; is_new_user = True
    if uid not in db["users"]: db["users"][uid] = {"points": 0}; save_db(db)
    resp = make_response(render_template('index.html', device_id=uid,
                           points=db["users"][uid]["points"], logs=db["logs"][-5:]))
    if is_new_user: resp.set_cookie('user_uuid', uid, max_age=31536000)
    return resp

@app.route('/api/status')
def get_status():
    uid = get_user_id(); db = load_db()
    return jsonify({"state": session_data["state"], "session": session_data["count"],
                    "points": db["users"][uid]["points"], "slots": [slot_status[i] for i in range(4)],
                    "is_my_session": session_data["active_user"] == uid,
                    "last_bottle_msg": session_data.get("last_bottle_msg", "")})

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
        db = load_db(); added = session_data["count"]
        if added > 0:
            db["users"][uid]["points"] += added; db["total_bottles"] += added
            db["logs"].append([time.strftime("%H:%M"), f"+{added} Pts", "Recycle"]); save_db(db)
        session_data.update({"state": "IDLE", "count": 0, "active_user": None})
        return jsonify({"status": "ok"})
    return jsonify({"status": "unauthorized"}), 401

@app.route('/api/emergency_reset')
def admin_reset():
    system_refresh(); return jsonify({"status": "system_refreshed"})

def system_refresh():
    for i in range(4): slot_status[i] = 0
    for pin in PINS_RELAYS: gpio_write(pin, 0)
    session_data.update({"state": "IDLE", "count": 0, "active_user": None}); beep(3)

@app.route('/api/admin_stats')
def admin_stats():
    if request.args.get('pass') != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    db = load_db()
    return jsonify({"total_bottles": db.get("total_bottles", 0),
                    "users": [{"user_id": k, "points": v.get("points", 0)} for k, v in db["users"].items()]})

@app.route('/redeem/<int:slot>/<int:pts>')
def redeem(slot, pts):
    uid = get_user_id(); db = load_db()
    if db["users"][uid]["points"] >= pts:
        db["users"][uid]["points"] -= pts
        db["logs"].append([time.strftime("%H:%M"), f"-{pts} Pts", SLOT_NAMES[slot]]); save_db(db)
        start_or_extend_relay(slot, pts * 300)
        beep(2)
    return redirect(url_for('index'))

# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)

    gpio_setup(PIN_BUZZER, "out", "0"); gpio_write(PIN_BUZZER, 0)
    gpio_setup(PIN_SERVO,  "out", "0"); gpio_write(PIN_SERVO,  0)
    for p in PINS_RELAYS: gpio_setup(p, "out", "0"); gpio_write(p, 0)

    init_lcd()
    lcd_write(["     RAYCHARGE    ", "   Initializing...  ", "                    ", "                    "])

    print("Setting pull-ups...")
    gpio_setup(PIN_IR_BOTTOM,   "in", pull="up")
    gpio_setup(PIN_IR_TOP,      "in", pull="up")
    gpio_setup(PIN_BTN_START,   "in", pull="up")
    gpio_setup(PIN_BTN_SELECT,  "in", pull="up")
    gpio_setup(PIN_BTN_CONFIRM, "in", pull="up")

    time.sleep(0.5)
    servo_init()
    threading.Thread(target=servo_pwm_thread, daemon=True).start()
    servo_goto(90, hold=0.5)

    lcd_write(["     RAYCHARGE     ", "    PRESS START     ", "                    ", "                    "])

    threading.Thread(target=lcd_writer_thread, daemon=True).start()
    threading.Thread(target=hardware_loop,     daemon=True).start()
    threading.Thread(target=display_manager,   daemon=True).start()
    app.run(host='0.0.0.0', port=80)
