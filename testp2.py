import time, os

# --- PINS ---
PIN_BTN   = "110"  # Physical Pin 3
PIN_IR    = "6"    # Physical Pin 7 (Bottom IR)
PIN_SERVO = "73"   # Physical Pin 22

def gpio_setup(pin, direction="in", value="0"):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        with open("/sys/class/gpio/export", "w") as f: f.write(pin)
    time.sleep(0.1)
    with open(f"{path}/direction", "w") as f: f.write(direction)
    if direction == "out":
        with open(f"{path}/value", "w") as f: f.write(value)

def gpio_read(pin):
    with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
        return int(f.read().strip())

def gpio_write(pin, val):
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write(str(val))

def move_servo(degrees):
    """Software PWM: 50Hz. 1ms = 0°, 2ms = 180°"""
    print(f"Moving Servo to {degrees} degrees...")
    pulse_ms = 1.0 + (degrees / 180.0) * 1.0
    period = 0.02
    off_time = period - (pulse_ms / 1000.0)
    
    # Send pulses for 1 second to give servo time to reach position
    for _ in range(50):
        gpio_write(PIN_SERVO, 1)
        time.sleep(pulse_ms / 1000.0)
        gpio_write(PIN_SERVO, 0)
        time.sleep(off_time)

# --- EXECUTION ---
try:
    print("Setting up GPIO...")
    gpio_setup(PIN_BTN, "in")
    gpio_setup(PIN_IR, "in")
    gpio_setup(PIN_SERVO, "out", "0")

    print("--- HARDWARE TEST STARTED ---")
    print("1. Block the IR sensor to see status.")
    print("2. Press the Button to trigger the Servo.")
    
    while True:
        btn_state = gpio_read(PIN_BTN)
        ir_state = gpio_read(PIN_IR)

        # IR Status (Active LOW means 0 = Object Detected)
        status_msg = "OBJECT DETECTED" if ir_state == 0 else "Path Clear"
        print(f"IR Status: {status_msg} | Button: {btn_state}", end="\r")

        # Button Trigger (Active LOW)
        if btn_state == 0:
            print("\nButton Pressed! Testing Servo...")
            move_servo(180)
            time.sleep(1)
            move_servo(0)
            print("Test Complete. Waiting for next press...")
            time.sleep(0.5)

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\nTest stopped by user.")
