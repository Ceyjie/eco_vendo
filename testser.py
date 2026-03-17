import time, os, mmap, struct

# Raw Memory Config for Orange Pi PA10
PA_BASE, PA_DAT_OFF, PA10_BIT = 0x01C20800, 0x10, (1 << 10)
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=PA_BASE & ~0xFFF)

def set_pin(val):
    off = (PA_BASE & 0xFFF) + PA_DAT_OFF
    cur = struct.unpack_from("<I", mem, off)[0]
    struct.pack_into("<I", mem, off, cur | PA10_BIT if val else cur & ~PA10_BIT)

def move(deg):
    print(f"Moving to {deg}...")
    pulse_ms = 0.5 + (deg / 180.0) * 2.0
    for _ in range(100): # 2 seconds of power
        t0 = time.perf_counter()
        set_pin(1)
        while (time.perf_counter() - t0) < (pulse_ms/1000.0): pass
        set_pin(0)
        while (time.perf_counter() - t0) < 0.020: pass

try:
    while True:
        move(90)
        time.sleep(2)
        move(0)
        time.sleep(2)
except KeyboardInterrupt:
    set_pin(0)
