import time, sys, tty, termios, os, threading, select

# --- SERVO PIN ---
PIN_SERVO = "20"  # PA20 - Physical pin 38

# --- GPIO HELPERS ---
def gpio_setup(pin, direction="out", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f: f.write(pin)
        except: pass
    time.sleep(0.1)
    try:
        with open(f"{path}/direction", "w") as f: f.write(direction)
        if direction == "out":
            with open(f"{path}/value", "w") as f: f.write(value)
    except Exception as e:
        print(f"GPIO setup error: {e}")

def gpio_write(pin, val):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f: f.write(str(val))
    except: pass

# --- CONTINUOUS PWM SERVO ---
# Runs in background thread, continuously sending pulses
# Standard servo: 50Hz, 1ms=0deg, 1.5ms=90deg, 2ms=180deg
servo_degrees  = 0
servo_running  = True

def pwm_thread():
    """Background thread: sends continuous 50Hz PWM to servo."""
    while servo_running:
        deg = servo_degrees
        pulse_sec = (1.0 + (deg / 180.0) * 1.0) / 1000.0  # 0.001s to 0.002s
        gpio_write(PIN_SERVO, 1)
        time.sleep(pulse_sec)
        gpio_write(PIN_SERVO, 0)
        time.sleep(0.02 - pulse_sec)  # Remainder of 20ms period

def servo_goto(degrees, hold_sec=0.6):
    """Set target angle and hold for hold_sec seconds."""
    global servo_degrees
    servo_degrees = max(0, min(180, degrees))
    print(f"  -> {servo_degrees} deg")
    time.sleep(hold_sec)

# --- KEYBOARD ---
def get_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def flush_input():
    while select.select([sys.stdin], [], [], 0)[0]:
        sys.stdin.read(1)

# --- MAIN ---
if __name__ == "__main__":
    print(f"Setting up servo on GPIO {PIN_SERVO} (PA20 Pin 38)...")
    gpio_setup(PIN_SERVO, "out", "0")

    # Start PWM background thread
    t = threading.Thread(target=pwm_thread, daemon=True)
    t.start()
    print("Servo PWM started.")
    print()
    print("  Running non-stop. Press Q to quit.")
    print()

    servo_goto(0)  # Home on start

    try:
        while True:
            # Check if Q pressed (non-blocking)
            if select.select([sys.stdin], [], [], 0)[0]:
                key = get_key().lower()
                if key == 'q':
                    break

            print("  Pushing right...")
            servo_goto(180, hold_sec=0.5)
            print("  Returning left...")
            servo_goto(0, hold_sec=0.5)

    finally:
        servo_running = False
        servo_goto(0, hold_sec=0.5)
        gpio_write(PIN_SERVO, 0)
        print("Done.")
