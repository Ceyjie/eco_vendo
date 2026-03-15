import time
import board
import digitalio
import adafruit_hx711

# Try to use the PA naming convention
try:
    data_pin = digitalio.DigitalInOut(board.PA8)   # Physical Pin 31
    clock_pin = digitalio.DigitalInOut(board.PA9)  # Physical Pin 33
except AttributeError:
    # Fallback if your board definition uses generic numbers
    # For Orange Pi One, PA8 is often 8 and PA9 is 9 in Blinka
    data_pin = digitalio.DigitalInOut(board.D8)
    clock_pin = digitalio.DigitalInOut(board.D9)

clock_pin.direction = digitalio.Direction.OUTPUT
data_pin.direction = digitalio.Direction.INPUT

# Initialize the library
hx = adafruit_hx711.HX711(data_pin, clock_pin)

print("Starting HX711 test...")

try:
    while True:
        # The HX711 goes LOW on the data pin when a reading is ready
        if not data_pin.value:
            print(f"Reading: {hx.value}")
        else:
            print("Sensor not ready...")
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
