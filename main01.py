import time, threading, os, subprocess
from flask import Flask, jsonify, render_template
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM, PIN_IR_TOP = "6", "1"
PIN_BUZZER = "0"
PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM = "13", "14", "110"
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# --- STATE ---
session_data = {
    "state": "IDLE", 
    "count": 0, 
    "selected_slot": 0,
    "last_activity": time.time()
}
slot_status = {0: 0, 1: 0, 2: 0, 3: 0}

# --- GPIO HELPERS ---
def gpio_setup(pin, direction="in", value="0"):
    if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: return
    time.sleep(0.1)
    with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(value)

def gpio_read(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f: return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

def beep(times=1):
    def run_beep():
        for _ in range(times):
            gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0); time.sleep(0.05)
    threading.Thread(target=run_beep).start() # Beep without blocking the main loop

# --- LCD ---
lcd = None
def init_lcd():
    global lcd
    for addr in [0x27, 0x3f]:
        try:
            lcd = CharLCD('PCF8574', addr, port=0, cols=20, rows=4, charmap='A00')
            lcd.clear()
            return
        except: lcd = None

def lcd_write(lines):
    if not lcd: return
    try:
        lcd.clear()
        for i, line in enumerate(lines[:4]):
            lcd.cursor_pos = (i, 0)
            lcd.write_string(line[:20])
    except: init_lcd()

def format_time(seconds):
    mins, secs = divmod(seconds, 60)
    if mins >= 60:
        hrs, mins = divmod(mins, 60)
        return f"{hrs:02d}:{mins:02d}"
    return f"{mins:02d}:{secs:02d}"

# --- RELAY TIMER ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 1)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 0)
    slot_status[slot] = 0

# --- LOGIC HANDLERS ---
def reset_activity():
    session_data["last_activity"] = time.time()

def handle_press(pin):
    reset_activity()
    state = session_data["state"]
    
    if pin == PIN_BTN_START and state == "IDLE":
        beep(1)
        session_data.update({"count": 0, "state": "INSERTING"})
        
    elif pin == PIN_BTN_SELECT and state == "SELECTING":
        beep(1)
        session_data["selected_slot"] = (session_data["selected_slot"] + 1) % 4
        
    elif pin == PIN_BTN_CONFIRM:
        if state == "INSERTING":
            beep(1)
            session_data["state"] = "SELECTING" if session_data["count"] > 0 else "IDLE"
        elif state == "SELECTING":
            slot = session_data["selected_slot"]
            total_sec = session_data["count"] * 300
            beep(2)
            threading.Thread(target=run_relay_timer, args=(slot, total_sec), daemon=True).start()
            session_data.update({"state": "IDLE", "count": 0})

# --- LOOPS ---
def hardware_loop():
    btn_pins = [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]
    last_val = {p: 1 for p in btn_pins}
    last_press_time = {p: 0 for p in btn_pins}
    debounce_delay = 0.2 

    while True:
        current_time = time.time()
        
        # 1. Check Buttons (Responsive Debounce)
        for p in btn_pins:
            val = gpio_read(p)
            if val == 0 and last_val[p] == 1: # Transition High -> Low
                if (current_time - last_press_time[p]) > debounce_delay:
                    handle_press(p)
                    last_press_time[p] = current_time
            last_val[p] = val

        # 2. Check IR Sensors
        if session_data["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                beep(1)
                reset_activity()
                time.sleep(0.6) # Minimal block for sensor reset
        
        # 3. Timeout Check
        if session_data["state"] != "IDLE":
            if (current_time - session_data["last_activity"]) > 60:
                session_data.update({"state": "IDLE", "count": 0})
                beep(3)

        time.sleep(0.01) # High frequency polling (100Hz)

def display_manager():
    last_disp_state = None
    while True:
        # Create a unique "fingerprint" of the current screen data
        current_disp_state = (
            session_data["state"], 
            session_data["count"], 
            session_data["selected_slot"],
            tuple(slot_status.values()) # Include timers in the check
        )

        if current_disp_state != last_disp_state:
            s, c, sl = session_data["state"], session_data["count"], session_data["selected_slot"]
            if s == "IDLE":
                t1 = f"U1:{format_time(slot_status[0])} U2:{format_time(slot_status[1])}"
                t2 = f"U3:{format_time(slot_status[2])} AC:{format_time(slot_status[3])}"
                lcd_write(["      ECO VENDO", "     PRESS START", t1, t2])
            elif s == "INSERTING":
                lcd_write(["   INSERT BOTTLE", f"   BOTTLES: {c}", f"   TIME: {c*5}m", "B3:CONFIRM"])
            elif s == "SELECTING":
                lcd_write(["      SELECT", f"    > {SLOT_NAMES[sl]}", f"    FOR {c*5} MINS", "B3:CONFIRM"])
            
            last_disp_state = current_disp_state
        
        time.sleep(0.2)

# --- FLASK ---
app = Flask(__name__)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def get_status():
    return jsonify({
        "state": session_data["state"],
        "count": session_data["count"],
        "timers": [format_time(slot_status[i]) for i in range(4)]
    })

if __name__ == '__main__':
    subprocess.run(["sudo", "fuser", "-k", "80/tcp"], capture_output=True)
    init_lcd()
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0")
    gpio_setup(PIN_BUZZER, "out", "0")
    
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
