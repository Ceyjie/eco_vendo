import time, threading, os, subprocess, sqlite3
from flask import Flask
from RPLCD.i2c import CharLCD

# --- CONFIG ---
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"    # Button 1
PIN_BTN_SELECT  = "14"    # Button 2
PIN_BTN_CONFIRM = "110"   # Button 3

PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]
SECONDS_PER_POINT = 300  # 5 Minutes per bottle

# --- STATE ---
session_data = {"count": 0, "active_points": 0}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE", "selected_slot": 0}

# --- GPIO HELPERS (ACTIVE HIGH) ---
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

# --- LCD ---
try:
    lcd = CharLCD('PCF8574', 0x27, port=0, cols=20, rows=4, charmap='A00')
except:
    try: lcd = CharLCD('PCF8574', 0x3f, port=0, cols=20, rows=4, charmap='A00')
    except: lcd = None

def lcd_write(lines):
    if not lcd: return
    try:
        lcd.clear()
        for i, line in enumerate(lines[:4]):
            lcd.cursor_pos = (i, 0)
            lcd.write_string(line[:20])
    except: pass

def format_time(seconds):
    return f"{seconds // 60}:{seconds % 60:02d}"

# --- RELAY TIMER ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 1) # RELAY ON (Active High)
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 0) # RELAY OFF
    slot_status[slot] = 0

# --- HARDWARE LOOP ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    
    while True:
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                
                # STEP 1: START BUTTON (Begin Session)
                if p == PIN_BTN_START and ui_state["state"] == "IDLE":
                    ui_state["state"] = "INSERTING"
                    session_data["count"] = 0
                    gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)

                # STEP 2: SELECT BUTTON (Cycle USBs)
                elif p == PIN_BTN_SELECT:
                    # Can be pressed during IDLE or during SLOT_PICK
                    ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
                    gpio_write(PIN_BUZZER, 1); time.sleep(0.05); gpio_write(PIN_BUZZER, 0)

                # STEP 3: CONFIRM BUTTON (Two roles)
                elif p == PIN_BTN_CONFIRM:
                    if ui_state["state"] == "INSERTING":
                        # First Confirm: Lock in the amount
                        session_data["active_points"] = session_data["count"]
                        ui_state["state"] = "SLOT_PICK"
                        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                    
                    elif ui_state["state"] == "SLOT_PICK":
                        # Second Confirm: Final activation
                        pts = session_data["active_points"]
                        slot = ui_state["selected_slot"]
                        if pts > 0:
                            total_sec = pts * SECONDS_PER_POINT
                            threading.Thread(target=run_relay_timer, args=(slot, total_sec), daemon=True).start()
                            lcd_write(["   POWER ACTIVE!", f" Slot: {SLOT_NAMES[slot]}", f" Time: {pts*5} mins", "   THANK YOU!"])
                            gpio_write(PIN_BUZZER, 1); time.sleep(0.5); gpio_write(PIN_BUZZER, 0)
                            time.sleep(3)
                        
                        # EMPTY POINTS & Reset
                        session_data["count"] = 0
                        session_data["active_points"] = 0
                        ui_state["state"] = "IDLE"

                time.sleep(0.2)
            last[p] = val

        # IR Counting Logic
        if ui_state["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                time.sleep(0.7)
        time.sleep(0.05)

def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            lcd_write(["--- ECO VENDO ---", "Press START to begin", "U1:{} U2:{}".format(format_time(slot_status[0]), format_time(slot_status[1])), "U3:{} AC:{}".format(format_time(slot_status[2]), format_time(slot_status[3]))])
        
        elif ui_state["state"] == "INSERTING":
            lcd_write(["   INSERT BOTTLE", f"Points: {session_data['count']}", "", "Press [CONFIRM]"])
            
        elif ui_state["state"] == "SLOT_PICK":
            lcd_write(["SELECT USB/PORT", f"Points: {session_data['active_points']}", f"Target: {SLOT_NAMES[ui_state['selected_slot']]}", "Press [CONFIRM]"])
            
        time.sleep(0.5)

# --- FLASK ---
app = Flask(__name__)
@app.route('/')
def index(): return "<h1>Eco Vendo Active</h1>"

if __name__ == '__main__':
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: gpio_setup(p, "in")
    for p in PINS_RELAYS: gpio_setup(p, "out", "0") 
    gpio_setup(PIN_BUZZER, "out", "0")
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    app.run(host='0.0.0.0', port=80)
