from flask import Flask, render_template_string, request, jsonify
import time, os, threading, mmap, struct, queue

app = Flask(__name__)

# --- CONFIG ---
PIN_SERVO    = "10"
PULSE_MIN_MS = 0.5
PULSE_MAX_MS = 2.5
PERIOD_MS    = 20.0

# Command queue
cmd_queue = queue.Queue(maxsize=1)

# --- /dev/mem direct register (Allwinner H3) ---
PA_BASE    = 0x01C20800
PA_DAT_OFF = 0x10
PA10_BIT   = (1 << 10)
_mem       = None
USE_DEVMEM = False

def init_devmem():
    global _mem, USE_DEVMEM
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        _mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                         mmap.PROT_READ | mmap.PROT_WRITE,
                         offset=PA_BASE & ~0xFFF)
        os.close(fd)
        USE_DEVMEM = True
        print("Using /dev/mem — high accuracy PWM.")
    except Exception as e:
        USE_DEVMEM = False
        print(f"/dev/mem unavailable ({e}), using sysfs.")

def gpio_setup():
    init_devmem()
    if not USE_DEVMEM:
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
        except Exception as e:
            print(f"GPIO sysfs error: {e}")

def gpio_set(val):
    if USE_DEVMEM:
        off = (PA_BASE & 0xFFF) + PA_DAT_OFF
        cur = struct.unpack_from("<I", _mem, off)[0]
        struct.pack_into("<I", _mem, off, cur | PA10_BIT if val else cur & ~PA10_BIT)
    else:
        try:
            with open(f"/sys/class/gpio/gpio{PIN_SERVO}/value", "w") as f:
                f.write("1" if val else "0")
        except: pass

def send_pulse(pulse_ms):
    pulse_s  = pulse_ms  / 1000.0
    period_s = PERIOD_MS / 1000.0
    t0 = time.perf_counter()
    gpio_set(1)
    while (time.perf_counter() - t0) < pulse_s: pass
    gpio_set(0)
    while (time.perf_counter() - t0) < period_s: pass

def servo_worker():
    """FIXED: Continually pulses until a NEW command is received."""
    pulse_ms = 1.5
    while True:
        try:
            # Check for new target, if none, keep the old pulse_ms
            pulse_ms = cmd_queue.get_nowait()
        except queue.Empty:
            pass
        
        send_pulse(pulse_ms)
        time.sleep(0.001)

# --- HTML WITH SWEEP FEATURE ---
HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Servo Pro Control</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Share+Tech+Mono&display=swap');
        body { font-family: 'Rajdhani', sans-serif; background: #0d0d0f; color: #e0e0e0; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; margin:0; }
        .panel { width: 100%; max-width: 420px; background: #16161a; border: 1px solid #2a2a30; border-radius: 20px; padding: 30px; box-shadow: 0 0 40px rgba(0,0,0,0.7); text-align: center; }
        h1 { color: #f5a623; letter-spacing: 3px; font-size: 20px; }
        .display { font-family: 'Share Tech Mono', monospace; font-size: 80px; color: #f5a623; line-height: 1; text-shadow: 0 0 24px rgba(245,166,35,0.4); margin: 20px 0; }
        input[type=range] { -webkit-appearance: none; width:100%; height:10px; border-radius:5px; outline:none; background: #2a2a30; margin: 20px 0; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:34px; height:34px; background:#f5a623; border-radius:50%; cursor:pointer; }
        .presets { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:20px; }
        .btn { padding:12px; font-family:'Rajdhani',sans-serif; font-weight:700; background:#1e1e24; color:#888; border:1px solid #2a2a30; border-radius:10px; cursor:pointer; transition: 0.2s; }
        .btn:hover { border-color:#f5a623; color:#f5a623; }
        .sweep-btn { background: #f5a623; color: #000; grid-column: span 4; margin-top: 10px; font-size: 18px; }
    </style>
</head>
<body>
<div class="panel">
    <h1>MG996R SYSTEM</h1>
    <div class="display" id="display">90°</div>
    <input type="range" min="0" max="180" value="90" id="slider" oninput="onSlide(this.value)" onchange="sendAngle(this.value)">
    
    <div class="presets">
        <button class="btn" onclick="setAngle(0)">0°</button>
        <button class="btn" onclick="setAngle(90)">90°</button>
        <button class="btn" onclick="setAngle(180)">180°</button>
        <button class="btn" onclick="setAngle(slider.value)">REFRESH</button>
        <button class="btn sweep-btn" onclick="runSweep()">START FULL RANGE SWEEP</button>
    </div>
</div>

<script>
    async function runSweep() {
        const btn = document.querySelector('.sweep-btn');
        btn.disabled = true; btn.textContent = "SWEEPING...";
        
        for(let i=0; i<=180; i+=5) { setAngle(i); await new Promise(r => setTimeout(r, 50)); }
        for(let i=180; i>=0; i-=5) { setAngle(i); await new Promise(r => setTimeout(r, 50)); }
        setAngle(90);
        
        btn.disabled = false; btn.textContent = "START FULL RANGE SWEEP";
    }

    function onSlide(val) { document.getElementById('display').textContent = val + '°'; }
    function sendAngle(val) { fetch('/set?value='+val); }
    function setAngle(a) { 
        document.getElementById('slider').value = a; 
        onSlide(a); 
        sendAngle(a); 
    }
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/set")
def set_servo():
    val = int(request.args.get('value', 90))
    pmin, pmax = PULSE_MIN_MS, PULSE_MAX_MS
    pulse_ms = pmin + (val / 180.0) * (pmax - pmin)
    try: cmd_queue.get_nowait()
    except queue.Empty: pass
    cmd_queue.put(pulse_ms)
    return "OK"

if __name__ == "__main__":
    gpio_setup()
    threading.Thread(target=servo_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
