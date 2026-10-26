import json
import os
import sys
import time
import subprocess
from datetime import datetime, timedelta

MINER_FILE = 'miners.json'
HA_DEVICES_FILE = 'ha_devices.json'
GPIO_PINS_FILE = 'gpio_pins.json'
CONTROL_SCRIPT = os.path.join(os.path.dirname(__file__), 'control.sh')
BACKEND_DIR = os.path.dirname(__file__)

# Hier speichern wir zuletzt gesendete Befehle (nur im RAM)
last_sent = {}
# Letzter Schleifen-Zeitpunkt für Intervall-Prüfung (verhindert verpasste Minuten)
_last_loop_at = None


def load_miners():
    if not os.path.exists(MINER_FILE):
        return []
    with open(MINER_FILE) as f:
        return json.load(f)


def load_ha_devices():
    path = os.path.join(BACKEND_DIR, HA_DEVICES_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def load_gpio_pins():
    path = os.path.join(BACKEND_DIR, GPIO_PINS_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def now_str():
    return datetime.now().strftime('%H:%M')


def _parse_hhmm(raw):
    """'HH:MM' → (hour, minute) oder None."""
    try:
        s = str(raw or "").strip()
        if len(s) < 4 or ":" not in s:
            return None
        hh, mm = s.split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return None


def time_crossed(target_hhmm, prev_dt, now_dt):
    """
    True, wenn die Soll-Uhrzeit im Intervall (prev_dt, now_dt] liegt.

    Damit werden Termine auch getroffen, wenn die Schleife länger als 30–60 s
    dauert und die exakte Minute „übersprungen“ würde.
    """
    parsed = _parse_hhmm(target_hhmm)
    if not parsed or now_dt is None:
        return False
    h, m = parsed
    # Exakter Treffer in der aktuellen Minute (klassisch)
    if now_dt.hour == h and now_dt.minute == m:
        return True
    if prev_dt is None:
        return False
    # Sollzeit heute (bei Mitternacht-Übergang gestern/heute prüfen)
    for day_offset in (0, -1):
        base = (now_dt + timedelta(days=day_offset)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )
        if prev_dt < base <= now_dt:
            return True
    return False


def should_send(device_id, action):
    """Nur alle 12h pro Aktion senden"""
    key = (device_id, action)
    last = last_sent.get(key)
    if not last or datetime.now() - last >= timedelta(hours=12):
        last_sent[key] = datetime.now()
        return True
    return False

def run_control(ip, user, pw, ths):
    print(f'⚙ control.sh {ip} {ths}')
    result = subprocess.run(
        [CONTROL_SCRIPT, ip, user, pw, str(ths)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        print(f'KRITISCH: control.sh {ip} Code {result.returncode}: {detail}')
        return False
    return True

def run_api(miner, cmdtype):
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    from device_registry import execute_device_action

    ip = miner.get("ip")
    device = {
        "type": "miner",
        "ip": ip,
        "username": miner.get("username", "root"),
        "password": miner.get("password", ""),
    }
    print(f"⛓ {cmdtype} → {ip}")
    ok = execute_device_action(device, cmdtype)
    if not ok:
        print(f"KRITISCH: {cmdtype} {ip} ohne Erfolg")
    return ok


def run_ha_action(entity_id, cmdtype):
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    from device_registry import execute_device_action

    device = {"type": "ha", "entity_id": entity_id, "id": entity_id}
    print(f'🔌 HA {cmdtype} → {entity_id}')
    ok = execute_device_action(device, cmdtype)
    if not ok:
        print(f"KRITISCH: HA {cmdtype} {entity_id} ohne Erfolg")
    return ok


def run_gpio_action(pin_entry, cmdtype):
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    from device_registry import execute_device_action

    pin_no = pin_entry.get("pin")
    gid = str(pin_entry.get("id") or (f"gpio:{pin_no}" if pin_no is not None else ""))
    device = {
        "type": "gpio",
        "id": gid,
        "ip": gid,
        "pin": pin_no,
        "label": pin_entry.get("label") or gid,
    }
    print(f'🧷 GPIO {cmdtype} → {gid} (Board {pin_no})')
    ok = execute_device_action(device, cmdtype)
    if not ok:
        print(f"KRITISCH: GPIO {cmdtype} {gid} ohne Erfolg")
    return ok


def scheduler_loop():
    global _last_loop_at
    print("⏱ Scheduler läuft und prüft alle 30 Sekunden...")
    print("   (Termine: Intervall-Treffer – auch bei längerer Schleife)")

    while True:
        now_dt = datetime.now()
        prev = _last_loop_at
        miners = load_miners()
        ha_devices = load_ha_devices()
        gpio_pins = load_gpio_pins()

        for miner in miners:
            ip = miner.get('ip')
            user = miner.get('username') or miner.get('user')
            pw = miner.get('password')

            if miner.get('time_pause'):
                if time_crossed(miner.get('pause_time'), prev, now_dt) and should_send(ip, 'pause'):
                    print(f'⏸ Zeit erreicht für PAUSE bei {ip} ({miner.get("pause_time")})')
                    run_api(miner, 'pause')

                if time_crossed(miner.get('resume_time'), prev, now_dt) and should_send(ip, 'resume'):
                    print(f'▶ Zeit erreicht für RESUME bei {ip} ({miner.get("resume_time")})')
                    run_api(miner, 'resume')

            if miner.get('time_ths'):
                if time_crossed(miner.get('low_time'), prev, now_dt) and should_send(ip, 'low'):
                    ths = miner.get('ths_low', '60')
                    print(f'⬇ Zeit erreicht für TH/s niedrig ({ths}) bei {ip}')
                    run_control(ip, user, pw, ths)

                if time_crossed(miner.get('high_time'), prev, now_dt) and should_send(ip, 'high'):
                    ths = miner.get('ths_high', '100')
                    print(f'⬆ Zeit erreicht für TH/s hoch ({ths}) bei {ip}')
                    run_control(ip, user, pw, ths)

        for dev in ha_devices:
            entity_id = dev.get('entity_id')
            if not entity_id or not dev.get('time_pause'):
                continue
            if time_crossed(dev.get('pause_time'), prev, now_dt) and should_send(entity_id, 'pause'):
                print(f'⏸ Zeit erreicht für AUS bei {entity_id}')
                run_ha_action(entity_id, 'pause')
            if time_crossed(dev.get('resume_time'), prev, now_dt) and should_send(entity_id, 'resume'):
                print(f'▶ Zeit erreicht für EIN bei {entity_id}')
                run_ha_action(entity_id, 'resume')

        for pin in gpio_pins:
            pin_no = pin.get('pin')
            if pin_no is None or not pin.get('time_pause'):
                continue
            gid = str(pin.get('id') or f"gpio:{pin_no}")
            if time_crossed(pin.get('pause_time'), prev, now_dt) and should_send(gid, 'pause'):
                print(f'⏸ Zeit erreicht für GPIO AUS bei {gid}')
                run_gpio_action(pin, 'pause')
            if time_crossed(pin.get('resume_time'), prev, now_dt) and should_send(gid, 'resume'):
                print(f'▶ Zeit erreicht für GPIO EIN bei {gid}')
                run_gpio_action(pin, 'resume')

        _last_loop_at = now_dt
        time.sleep(30)

        # Täglicher MAC-Scan für fehlende Miner (internes 24h-Intervall)
        try:
            if int(time.time()) % 600 < 30:
                from ip_recovery import run_daily_missing_scan

                run_daily_missing_scan()
        except Exception:
            pass


if __name__ == '__main__':
    scheduler_loop()
