import gpiod
import time

PIN_DT = 68
chip = gpiod.Chip("/dev/gpiochip0")
line = chip.get_line(PIN_DT)
line.request(consumer="diag", type=gpiod.LINE_REQ_DIR_IN)

print("Monitoring DT pin. It should be flickering or stay LOW if ready.")
for _ in range(20):
    print(f"DT Value: {line.get_value()}")
    time.sleep(0.2)
line.release()
