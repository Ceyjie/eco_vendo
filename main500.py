import time, threading, os, subprocess
from flask import Flask
from RPLCD.i2c import CharLCD

# --- CONFIGURATION ---
# GPIO Pins (Adjust these based on your specific board)
PIN_IR_BOTTOM = "6"
PIN_IR_TOP    = "1"
PIN_BUZZER    = "0"
PIN_BTN_START   = "13"    # Button 1: Start
PIN_BTN_SELECT  = "14"    # Button 2: Select USB
PIN_BTN_CONFIRM = "110"   # Button 3: Confirm

# Relay Pins (Active High: Logic 1 = ON)
PINS_RELAYS = ["3", "2", "67", "21"]
SLOT_NAMES = ["USB 1", "USB 2", "USB 3", "AC 220V"]

# Timing: 300 seconds = 5 minutes per bottle
SECONDS_PER_POINT = 300  

# --- GLOBAL STATE ---
session_data = {"count": 0, "active_points": 0}
slot_status  = {0: 0, 1: 0, 2: 0, 3: 0}
ui_state     = {"state": "IDLE", "selected_slot": 0}

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
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except: return 1

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# --- LCD INITIALIZATION ---
# Checks common I2C addresses (0x27 and 0x3f)
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

# --- RELAY TIMER THREAD ---
def run_relay_timer(slot, seconds):
    gpio_write(PINS_RELAYS[slot], 1) # Turn Relay ON
    while seconds > 0:
        slot_status[slot] = seconds
        time.sleep(1)
        seconds -= 1
    gpio_write(PINS_RELAYS[slot], 0) # Turn Relay OFF
    slot_status[slot] = 0

# --- MAIN HARDWARE LOGIC ---
def hardware_loop():
    last = {PIN_BTN_START: 1, PIN_BTN_SELECT: 1, PIN_BTN_CONFIRM: 1}
    
    while True:
        for p in [PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]:
            val = gpio_read(p)
            if val == 0 and last[p] == 1:
                
                # STEP 1: START (Button 1)
                if p == PIN_BTN_START and ui_state["state"] == "IDLE":
                    ui_state["state"] = "INSERTING"
                    session_data["count"] = 0
                    gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)

                # STEP 2: SELECT (Button 2) - Cycles through the 4 ports
                elif p == PIN_BTN_SELECT:
                    ui_state["selected_slot"] = (ui_state["selected_slot"] + 1) % 4
                    gpio_write(PIN_BUZZER, 1); time.sleep(0.05); gpio_write(PIN_BUZZER, 0)

                # STEP 3: CONFIRM (Button 3) - Multi-stage action
                elif p == PIN_BTN_CONFIRM:
                    if ui_state["state"] == "INSERTING":
                        # Lock the bottle count and move to selection
                        session_data["active_points"] = session_data["count"]
                        ui_state["state"] = "SLOT_PICK"
                        gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                    
                    elif ui_state["state"] == "SLOT_PICK":
                        # Activate the relay and show the extended Thank You
                        pts = session_data["active_points"]
                        slot = ui_state["selected_slot"]
                        if pts > 0:
                            total_sec = pts * SECONDS_PER_POINT
                            threading.Thread(target=run_relay_timer, args=(slot, total_sec), daemon=True).start()
                            
                            # Screen 1: Confirmation
                            lcd_write([
                                "   POWER ACTIVE!",
                                f" Slot: {SLOT_NAMES[slot]}",
                                f" Time: {pts*5} mins",
                                "   ENJOY! :)"
                            ])
                            gpio_write(PIN_BUZZER, 1); time.sleep(0.4); gpio_write(PIN_BUZZER, 0)
                            time.sleep(2.5)

                            # Screen 2: Emotional Reward (Thank You)
                            lcd_write([
                                "   THANK YOU!   ",
                                "You helped protect",
                                "our environment by",
                                "recycling plastic!"
                            ])
                            time.sleep(5) # Long duration for readability
                        
                        # Reset session back to zero/idle
                        session_data["count"] = 0
                        session_data["active_points"] = 0
                        ui_state["state"] = "IDLE"

                time.sleep(0.2) # Debounce
            last[p] = val

        # IR Sensor Counting Logic
        if ui_state["state"] == "INSERTING":
            if gpio_read(PIN_IR_BOTTOM) == 0 and gpio_read(PIN_IR_TOP) == 0:
                session_data["count"] += 1
                gpio_write(PIN_BUZZER, 1); time.sleep(0.1); gpio_write(PIN_BUZZER, 0)
                time.sleep(0.7)
        time.sleep(0.05)

# --- DISPLAY MANAGER ---
def display_manager():
    while True:
        if ui_state["state"] == "IDLE":
            lcd_write([
                "--- ECO VENDO ---",
                "Press START to begin",
                "U1:{} U2:{}".format(format_time(slot_status[0]), format_time(slot_status[1])),
                "U3:{} AC:{}".format(format_time(slot_status[2]), format_time(slot_status[3]))
            ])
        elif ui_state["state"] == "INSERTING":
            lcd_write([
                "   INSERT BOTTLE",
                f"Points: {session_data['count']}",
                "",
                "Press [CONFIRM]"
            ])
        elif ui_state["state"] == "SLOT_PICK":
            lcd_write([
                "SELECT USB/PORT",
                f"Points: {session_data['active_points']}",
                f"Target: {SLOT_NAMES[ui_state['selected_slot']]}",
                "Press [CONFIRM]"
            ])
        time.sleep(0.5)

# --- FLASK WEB SERVER ---
app = Flask(__name__)
@app.route('/')
def index(): return "<h1>Eco Vendo Active</h1>"

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # Initialize Pins
    for p in [PIN_IR_BOTTOM, PIN_IR_TOP, PIN_BTN_START, PIN_BTN_SELECT, PIN_BTN_CONFIRM]: 
        gpio_setup(p, "in")
    for p in PINS_RELAYS: 
        gpio_setup(p, "out", "0") # Relays start OFF
    gpio_setup(PIN_BUZZER, "out", "0")
    
    # Start Background Threads
    threading.Thread(target=hardware_loop, daemon=True).start()
    threading.Thread(target=display_manager, daemon=True).start()
    
    # Run Web App
    app.run(host='0.0.0.0', port=80)
