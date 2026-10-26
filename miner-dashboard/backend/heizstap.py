#!/usr/bin/env python3
# heizstap_combo.py

import os, json, time, threading
from datetime import datetime

# ---------- GPIO ----------
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except Exception as e:
    print(f"[WARNUNG] GPIO nicht verfügbar: {e}")
    GPIO_AVAILABLE = False

# ---------- Pfade ----------
BASE_DIR = os.path.dirname(__file__)
COMMANDS_FILE = os.path.join(BASE_DIR, "commands.json")
# Nicht miner_status.json — das gehört status.py (Miner running/paused).
STATUS_FILE   = os.path.join(BASE_DIR, "gpio_pin_status.json")

# ---------- Heizstab ----------
PIN_MAP = {
    3: 2,
    5: 3,
    7: 4,
    11: 17,
    13: 27,
    15: 22,
    19: 10,
    21: 9
}
CONTROL_PINS = list(PIN_MAP.keys())
last_state = {}

# ---------- Dimmer ----------
ZC_BOARD = 37
OUT_BOARDS = [36, 38, 40]
BOARD2BCM_EXTRA = {36:16, 37:26, 38:20, 40:21}

MAINS_HZ = 50
WINDOW_S = 2.0
WINDOW_POS_COUNT = int(MAINS_HZ * WINDOW_S)
GATE_PULSE_S = 0.009

lock = threading.Lock()
allow_percent = 0
accum = 0

# ---------- Heizstufen ----------
heating_stage = 0
last_stage_change_ts = 0.0
STAGE_DELAY = 30.0

# ---------- Utils ----------
def load_json(p):
    if not os.path.exists(p): return {}
    try:
        with open(p) as f:
            t = f.read().strip()
            return json.loads(t) if t else {}
    except:
        return {}

def save_json(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=4)
    os.replace(tmp, p)

def write_command(pin, action):
    data = load_json(COMMANDS_FILE)
    data[str(pin)] = {
        "action": action,
        "timestamp": datetime.now().isoformat()
    }
    save_json(COMMANDS_FILE, data)
    print(f"[CMD] Pin {pin} → {action.upper()}")

# ---------- GPIO ----------
def setup_gpio():
    if not GPIO_AVAILABLE:
        return
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    for b, g in PIN_MAP.items():
        GPIO.setup(g, GPIO.OUT, initial=GPIO.LOW)
    for b in OUT_BOARDS:
        GPIO.setup(BOARD2BCM_EXTRA[b], GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(BOARD2BCM_EXTRA[ZC_BOARD], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.add_event_detect(
        BOARD2BCM_EXTRA[ZC_BOARD],
        GPIO.RISING,
        callback=zc_callback,
        bouncetime=2
    )

# ---------- Heizstab ----------
def set_pin(board_pin, action):
    bcm = PIN_MAP[board_pin]
    if GPIO_AVAILABLE:
        GPIO.output(bcm, GPIO.HIGH if action == "on" else GPIO.LOW)
    last_state[board_pin] = action

def update_status():
    now = datetime.now().isoformat()
    status = []
    for b, g in PIN_MAP.items():
        if GPIO_AVAILABLE:
            s = GPIO.input(g)
            # HIGH = on (set_pin) → running; LOW = off → paused
            st = "running" if s else "paused"
        else:
            act = last_state.get(b, "unknown")
            st = "running" if act == "on" else ("paused" if act == "off" else "unknown")
        status.append({"pin": b, "status": st, "timestamp": now})
    save_json(STATUS_FILE, status)


def _dashboard_managed_pins():
    """Pins aus Schaltbare Gibbo-Pins — nicht von der Heizlogik überschreiben."""
    path = os.path.join(BASE_DIR, "gpio_pins.json")
    managed = set()
    try:
        data = load_json(path)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                pin = item.get("pin")
                if pin is None:
                    continue
                managed.add(int(pin))
    except Exception:
        pass
    return managed

def process_commands():
    data = load_json(COMMANDS_FILE)
    for p in CONTROL_PINS:
        if str(p) in data:
            set_pin(p, data[str(p)]["action"])

def heizstab_thread():
    while True:
        process_commands()
        update_status()
        time.sleep(0.4)

# ---------- Energie ----------
def read_power_w():
    p = os.path.join(BASE_DIR, "energy_value.txt")
    if not os.path.exists(p): return None
    try:
        return float(open(p).read().strip()) * 1000
    except:
        return None

# ---------- Dimmer Regelung ----------
last_regulation = 0.0

def regulate_setpoint():
    global allow_percent, last_regulation
    now = time.time()
    if now - last_regulation < 2.4:
        return
    last_regulation = now

    w = read_power_w()
    if w is None:
        return

    if w < -300:
        allow_percent = min(100, allow_percent + 1)
        action = "+1%"
    elif w > 0:
        allow_percent = max(0, allow_percent - 1)
        action = "-1%"
    else:
        action = "HALTEN"

    print(f"[Dimmer] Energy={w:.0f} W | Regler={allow_percent}% | {action}")
# ---------- Heizlogik ----------

def heating_logic():
    global last_stage_change_ts

    now = time.time()

    # 30s Sperrzeit nach JEDEM Schalten
    if now - last_stage_change_ts < STAGE_DELAY:
        return

    managed = _dashboard_managed_pins()
    # Merke aktuellen Zustand (so wie wir ihn gesetzt haben)
    pin3_on = (last_state.get(3) == "on")
    pin5_on = (last_state.get(5) == "on")

    # ---------- Regler = 100 % ----------
    if allow_percent == 100:

        # IMMER zuerst Pin 3 AUS
        if pin3_on and 3 not in managed:
            write_command(3, "off")
            last_stage_change_ts = now
            print("[Heizlogik] 100% → Pin 3 AUS")
            return

        # NUR wenn Pin 3 schon AUS ist → Pin 5 AUS
        if pin5_on and 5 not in managed:
            write_command(5, "off")
            last_stage_change_ts = now
            print("[Heizlogik] 100% → Pin 5 AUS")
            return

        return  # nichts mehr zu tun

    # ---------- Regler = 0 % ----------
    if allow_percent == 0:

        # IMMER zuerst Pin 3 EIN
        if not pin3_on and 3 not in managed:
            write_command(3, "on")
            last_stage_change_ts = now
            print("[Heizlogik] 0% → Pin 3 EIN")
            return

        # NUR wenn Pin 3 schon EIN ist → Pin 5 EIN
        if not pin5_on and 5 not in managed:
            write_command(5, "on")
            last_stage_change_ts = now
            print("[Heizlogik] 0% → Pin 5 EIN")
            return

        return  # nichts mehr zu tun


# ---------- Dimmer Timing ----------
def should_pass():
    global accum
    accum += allow_percent
    if accum >= 100:
        accum -= 100
        return True
    return False

def fire_outputs_once():
    for b in OUT_BOARDS:
        GPIO.output(BOARD2BCM_EXTRA[b], GPIO.HIGH)
    time.sleep(GATE_PULSE_S)
    for b in OUT_BOARDS:
        GPIO.output(BOARD2BCM_EXTRA[b], GPIO.LOW)

def zc_callback(channel):
    if should_pass():
        fire_outputs_once()

def dimmer_thread():
    while True:
        regulate_setpoint()
        heating_logic()
        time.sleep(0.01)

# ---------- Main ----------
def main():
    print("[START]")
    setup_gpio()
    threading.Thread(target=heizstab_thread, daemon=True).start()
    threading.Thread(target=dimmer_thread, daemon=True).start()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
