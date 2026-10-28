from dashboard_battery import load_battery_data, load_battery_soc
from dashboard_miner_status import load_miner_status_details, load_miner_status_map
from miner_flow_data import build_flow_response, compute_aktueller_verbrauch_kw
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from urllib.parse import quote
import json, os, subprocess, socket, threading

app = Flask(__name__)
app.secret_key = 'supersecretkey'

try:
    from ram_cache import setup_runtime_dirs

    setup_runtime_dirs(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass


def _wants_json() -> bool:
    if request.form.get("_ajax") == "1":
        return True
    if request.headers.get("X-Requested-With") == "fetch":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and best is not None


def _action_response(
    message: str,
    *,
    ok: bool = True,
    redirect_to: str = "/",
    extra: dict | None = None,
):
    if _wants_json():
        payload = {"ok": ok, "message": message}
        if extra:
            payload.update(extra)
        return jsonify(payload), (200 if ok else 400)
    if message:
        sep = "&" if "?" in redirect_to else "?"
        return redirect(f"{redirect_to}{sep}hinweis={quote(message)}")
    return redirect(redirect_to)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
MINER_FILE = os.path.join(_BACKEND_DIR, 'miners.json')
HA_DEVICES_FILE = os.path.join(_BACKEND_DIR, 'ha_devices.json')
SETTINGS_FILE = os.path.join(_BACKEND_DIR, 'dashboard_settings.json')
ZIELORDNER = os.path.join(_BACKEND_DIR, "shared_settings")

def load_settings():
    settings = {
        "avg_minutes": 45,
        "current_kw": "n/a",
        "avg_kw": "n/a",
        "smartmeter_url": "",
        "login_username": "admin",
        "login_password": "",
    }

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings.update(json.load(f))
        except Exception:
            pass

    try:
        with open(os.path.join(ZIELORDNER, "smartmeter_url.txt"), "r") as f:
            settings["smartmeter_url"] = f.read().strip()
    except Exception:
        pass

    return settings


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)


def _dashboard_actor() -> dict:
    settings = load_settings()
    return {
        "source": "dashboard",
        "username": session.get("username") or settings.get("login_username") or "Dashboard",
    }

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from control_algorithm.config import load_config, sort_ha_devices_by_priority, sort_miners_by_priority

    algo_config = load_config()
    try:
        from ip_recovery import ensure_all_miner_macs

        ensure_all_miner_macs()
    except Exception:
        pass
    with open(MINER_FILE) as f:
        miners = sort_miners_by_priority(json.load(f), algo_config)
    settings = load_settings()
    settings["grid_kw"] = _read_em_file("energy_value.txt")
    settings["avg_kw"] = _read_em_file("energy_avg.txt")
    bat = load_battery_data()
    settings["current_kw"] = (
        compute_aktueller_verbrauch_kw(os.path.dirname(__file__), bat.get("power_kw"))
        or "n/a"
    )
    battery_soc = load_battery_soc()
    miner_status = load_miner_status_map()
    try:
        from device_registry import get_ha_device_status_map, load_ha_devices_raw

        ha_devices = load_ha_devices_raw()
        ha_devices = sort_ha_devices_by_priority(ha_devices, algo_config)
        ha_status = get_ha_device_status_map()
    except Exception:
        ha_devices = []
        ha_status = {}
    try:
        from device_registry import get_gpio_status_map, load_gpio_pins_raw

        gpio_pins = load_gpio_pins_raw()
        pri = {
            str(d.get("ip") or ""): d.get("priority", 5)
            for d in (algo_config.get("devices") or [])
        }
        gpio_pins = sorted(
            gpio_pins,
            key=lambda g: (
                -float(pri.get(str(g.get("id") or ""), g.get("priority") or 5) or 5),
                int(g.get("pin") or 0),
            ),
        )
        gpio_status = get_gpio_status_map()
    except Exception:
        gpio_pins = []
        gpio_status = {}
    try:
        from ha_entity_mapping import get_visibility, load_config as load_ha_mapping

        energy_visibility = get_visibility(load_ha_mapping())
    except Exception:
        energy_visibility = {
            "pv": True,
            "hydro": True,
            "grid": True,
            "haus": True,
            "betrieb": True,
            "battery": True,
        }
    try:
        from ip_recovery import list_pending_recovery

        recovery_pending = list_pending_recovery()
    except Exception:
        recovery_pending = []
    return render_template(
        'dashboard.html',
        miners=miners,
        ha_devices=ha_devices,
        ha_status=ha_status,
        gpio_pins=gpio_pins,
        gpio_status=gpio_status,
        settings=settings,
        battery_soc=battery_soc,
        miner_status=miner_status,
        energy_visibility=energy_visibility,
        recovery_pending=recovery_pending,
        message=request.args.get('message'),
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    settings = load_settings()
    if request.method == 'POST':
        if request.form['username'] == settings['login_username'] and request.form['password'] == settings['login_password']:
            session['logged_in'] = True
            session['username'] = request.form['username']
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        settings = load_settings()
        settings['login_username'] = request.form['new_username']
        settings['login_password'] = request.form['new_password']
        save_settings(settings)
        return redirect('/')
    return render_template('settings.html')


@app.context_processor
def inject_nav_badges():
    try:
        from public_update import dashboard_status

        status = dashboard_status()
        return {
            "nav_update_count": int(status.get("pending_updates") or 0),
            "nav_public_update": status,
        }
    except Exception:
        return {"nav_update_count": 0, "nav_public_update": {}}


@app.route('/updates')
def updates_page():
    return render_template('updates.html', public_update=public_update_status())


def _network_info() -> dict:
    hostname = socket.gethostname()
    ipv4 = "n/a"
    iface = "n/a"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ipv4 = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True,
            timeout=3,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            addr = parts[3].split("/", 1)[0]
            if addr == ipv4 or iface == "n/a":
                iface = parts[1]
                if addr == ipv4:
                    break
    except Exception:
        pass
    status = "Verbunden" if ipv4 not in ("n/a", "127.0.0.1") else "Lokal / unbekannt"
    return {"hostname": hostname, "ipv4": ipv4, "iface": iface, "status": status}


@app.route('/netzwerk')
def netzwerk_page():
    return render_template('netzwerk.html', network=_network_info())


@app.route('/boersenhandel')
def boersenhandel_page():
    from boersenhandel import EXCHANGES, load_settings

    settings = load_settings()
    exchanges = [
        {"id": key, "label": spec["label"], "group": spec.get("group") or "Börsen"}
        for key, spec in EXCHANGES.items()
    ]
    return render_template("boersenhandel.html", settings=settings, exchanges=exchanges)


@app.route('/api/boersenhandel/prices')
def api_boersenhandel_prices():
    from boersenhandel import load_settings, price_view

    settings = load_settings()
    exchange = (request.args.get("exchange") or settings["exchange"]).strip()
    range_mode = (request.args.get("range") or "24h").strip()
    try:
        payload = price_view(exchange, range_mode, settings)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc) or "Börsenpreis nicht erreichbar."}), 502
    return jsonify({"ok": True, **payload})


@app.route('/api/boersenhandel/year')
def api_boersenhandel_year():
    from boersenhandel import year_view

    exchange = (request.args.get("exchange") or "").strip()
    try:
        payload = year_view(exchange)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc) or "Jahresverlauf nicht erreichbar."}), 502
    return jsonify({"ok": True, **payload})


@app.route('/api/boersenhandel/sparkline')
def api_boersenhandel_sparkline():
    from boersenhandel import sparkline

    try:
        points = sparkline()
    except Exception:
        points = []
    return jsonify({"ok": True, "points": points})


@app.route('/api/boersenhandel/settings', methods=['POST'])
def api_boersenhandel_settings():
    from boersenhandel import save_settings

    try:
        saved = save_settings(
            (request.form.get("exchange") or "").strip(),
            request.form.get("buy_ct_kwh") or request.form.get("lower_ct_kwh") or 0,
            request.form.get("stop_ct_kwh") or request.form.get("upper_ct_kwh") or 0,
            request.form.get("sell_ct_kwh") or 25,
            request.form.get("apply_bms") in ("1", "true", "on"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "message": "Börse und Schwellwerte gespeichert.", **saved})


@app.route('/energiegemeinschaft')
def energiegemeinschaft_page():
    from energiegemeinschaft import COMMUNITIES, load_settings

    settings = load_settings()
    communities = [
        {"id": key, "label": item["label"], "region": item["region"]}
        for key, item in COMMUNITIES.items()
    ]
    return render_template("energiegemeinschaft.html", settings=settings, communities=communities)


@app.route('/api/energiegemeinschaft')
def api_energiegemeinschaft():
    from boersenhandel import load_settings as load_market, price_view
    from energiegemeinschaft import comparison, load_settings

    settings = load_settings()
    community = (request.args.get("community") or settings["community"]).strip()
    market = load_market()
    try:
        prices = price_view(market["exchange"], "day", market)
        points = prices.get("points") or []
    except Exception:
        points = []
    payload = comparison(points, community)
    return jsonify({"ok": True, **payload})


@app.route('/api/energiegemeinschaft/settings', methods=['POST'])
def api_energiegemeinschaft_settings():
    from energiegemeinschaft import save_settings

    try:
        enabled = None
        if "enabled" in request.form:
            enabled = request.form.get("enabled") in ("1", "true", "on")
        saved = save_settings((request.form.get("community") or "").strip(), enabled)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "message": "Energiegemeinschaft gespeichert.", **saved})


@app.route('/wechselrichter-bms')
def wechselrichter_bms_page():
    # VORLÄUFIG TEMPORARY_BMS_TAKEOVER: Route und Seite nach der Übernahme entfernen.
    return render_template("wechselrichter_bms.html")


@app.route('/wechselrichter-bms/netzbezug')
def wechselrichter_bms_netzbezug():
    from bms_seite import load_settings as load_bms_test

    return render_template("bms_netzbezug.html", settings=load_bms_test())


@app.route('/api/bms-netzbezug')
def api_bms_netzbezug():
    from bms_seite import status

    return jsonify({"ok": True, **status(record=True)})


@app.route('/api/bms-netzbezug/settings', methods=['POST'])
def api_bms_netzbezug_settings():
    from bms_seite import save_settings as save_bms_test

    saved = save_bms_test(
        request.form.get("test_mode") in ("1", "true", "on"),
        request.form.get("target_grid_kw") or 0,
        request.form.get("peak_cap_kw") or 0,
    )
    return jsonify({"ok": True, "message": "Test-Modus gespeichert. Es wurde kein Befehl gesendet.", **saved})

@app.route('/smartmeter', methods=['GET', 'POST'])
def smartmeter():
    settings = load_settings()  # <-- DAS FEHLT aktuell bei GET
    if request.method == 'POST':
        url = request.form['smartmeter_url']
        os.makedirs(ZIELORDNER, exist_ok=True)
        with open(os.path.join(ZIELORDNER, "smartmeter_url.txt"), "w") as f:
            f.write(url)
        settings["smartmeter_url"] = url
        save_settings(settings)
        return redirect('/')
    return render_template('smartmeter.html', settings=settings)  # <-- Variable Ã¼bergeben!

@app.route('/add_miner', methods=['GET', 'POST'])
def add_miner():
    if request.method == 'POST':
        new_miner = {
            "ip": (request.form.get('ip') or '').strip(),
            "username": (request.form.get('username') or 'root').strip(),
            "password": request.form.get('password') or '',
            "model": (request.form.get('model') or '').strip(),
            "note": "",
            "auto": False,
        }
        if not new_miner["ip"]:
            return redirect('/add_miner?error=' + quote('IP-Adresse fehlt'))
        try:
            from ip_recovery import register_miner

            registered = register_miner(new_miner)
        except Exception as exc:
            return redirect('/add_miner?error=' + quote(str(exc)))
        ip = registered.get("ip", new_miner["ip"])
        mac = registered.get("mac")
        if mac:
            msg = f"Miner {ip} hinzugefügt — MAC {mac} automatisch ermittelt."
        else:
            msg = (
                f"Miner {ip} hinzugefügt — MAC wird im Hintergrund ermittelt "
                "(Gerät muss im Netz erreichbar sein)."
            )
        return redirect('/?message=' + quote(msg))
    error = request.args.get('error')
    return render_template('add_miner.html', error=error)

@app.route('/add_device', methods=['GET', 'POST'])
def add_device():
    from device_registry import default_ha_device, load_ha_devices_raw, save_ha_devices
    from gpio_pins import (
        PIN_MAP,
        available_pins_for_select,
        load_gpio_pins_raw,
    )
    from ha_entity_mapping import is_controllable_entity, list_ha_entities
    from urllib.parse import quote

    ha_devices = load_ha_devices_raw()
    gpio_pins = load_gpio_pins_raw()
    if request.method == 'POST':
        entity_id = (request.form.get('entity_id') or '').strip()
        if not entity_id:
            return redirect('/add_device')
        if not is_controllable_entity(entity_id):
            return redirect('/add_device?error=' + quote(
                'Dieses Objekt ist kein schaltbares Gerät (nur Steckdosen, Schalter, Lichter, …).'
            ))
        if any(d.get('entity_id') == entity_id for d in ha_devices):
            return redirect('/add_device')
        entities = {e['entity_id']: e for e in list_ha_entities()}
        ent = entities.get(entity_id, {})
        label = (request.form.get('label') or '').strip()
        if not label:
            label = f"{ent.get('device', '')} · {ent.get('name', entity_id)}".strip(' ·')
        new_dev = default_ha_device(
            entity_id,
            label=label,
            note=(request.form.get('note') or '').strip(),
        )
        ha_devices.append(new_dev)
        save_ha_devices(ha_devices)
        return redirect('/')
    existing_ids = [d.get('entity_id') for d in ha_devices if d.get('entity_id')]
    return render_template(
        'add_device.html',
        ha_devices=ha_devices,
        existing_ids=existing_ids,
        error=request.args.get('error'),
        gpio_pins=gpio_pins,
        available_gpio_pins=available_pins_for_select(gpio_pins),
        pin_bcm_map=PIN_MAP,
        gpio_error=request.args.get('gpio_error'),
    )


@app.route('/add_gpio_pin', methods=['POST'])
def add_gpio_pin():
    from control_algorithm.config import load_config, normalize_device_priority, save_config
    from gpio_pins import (
        AVAILABLE_PINS,
        default_gpio_pin,
        load_gpio_pins_raw,
        pin_id,
        save_gpio_pins,
    )
    from urllib.parse import quote

    try:
        pin = int(request.form.get('pin') or '')
    except ValueError:
        return redirect('/add_device?gpio_error=' + quote('Ungültiger Pin.') + '#gibbo-pins')
    if pin not in AVAILABLE_PINS:
        return redirect('/add_device?gpio_error=' + quote('Dieser Pin ist nicht verfügbar.') + '#gibbo-pins')
    pins = load_gpio_pins_raw()
    if any(int(p.get('pin') or -1) == pin for p in pins):
        return redirect('/add_device?gpio_error=' + quote('Pin bereits zugewiesen.') + '#gibbo-pins')
    try:
        power_kw = float(str(request.form.get('power_kw') or '0').replace(',', '.'))
    except ValueError:
        power_kw = 0.0
    priority = normalize_device_priority(request.form.get('priority', '5'))
    new_pin = default_gpio_pin(
        pin,
        label=(request.form.get('label') or '').strip(),
        note=(request.form.get('note') or '').strip(),
        power_kw=power_kw,
        priority=priority,
    )
    pins.append(new_pin)
    save_gpio_pins(pins)
    # In Steuerungs-Config eintragen (Last/Prio wie bei HA/Miner)
    try:
        cfg = load_config()
        devices = list(cfg.get('devices') or [])
        gid = pin_id(pin)
        devices = [d for d in devices if str(d.get('ip') or '') != gid]
        devices.append({
            'ip': gid,
            'type': 'gpio',
            'pin': pin,
            'enabled': False,
            'power_kw': power_kw,
            'priority': priority,
        })
        cfg['devices'] = devices
        save_config(cfg)
    except Exception:
        pass
    return redirect('/')


@app.route('/delete_gpio_pin', methods=['POST'])
def delete_gpio_pin():
    from control_algorithm.config import load_config, save_config
    from gpio_pins import load_gpio_pins_raw, parse_pin_id, pin_id, save_gpio_pins

    raw = request.form.get('pin_id', '')
    pin = parse_pin_id(raw)
    if pin is None:
        return redirect('/')
    gid = pin_id(pin)
    pins = [p for p in load_gpio_pins_raw() if parse_pin_id(p.get('pin') if p.get('pin') is not None else p.get('id')) != pin]
    save_gpio_pins(pins)
    try:
        cfg = load_config()
        cfg['devices'] = [d for d in (cfg.get('devices') or []) if str(d.get('ip') or '') != gid]
        save_config(cfg)
    except Exception:
        pass
    return redirect('/')


@app.route('/update_gpio_pin_note', methods=['POST'])
def update_gpio_pin_note():
    from gpio_pins import load_gpio_pins_raw, parse_pin_id, save_gpio_pins

    raw = request.form.get('pin_id', '')
    note = request.form.get('note', '')
    pin = parse_pin_id(raw)
    label = raw
    pins = load_gpio_pins_raw()
    for p in pins:
        if parse_pin_id(p.get('pin') if p.get('pin') is not None else p.get('id')) == pin:
            p['note'] = note
            label = p.get('label') or raw
            break
    save_gpio_pins(pins)
    return _action_response(f"Beschreibung für {label} gespeichert.")


@app.route('/gpio_pin_action', methods=['POST'])
def gpio_pin_action():
    from device_registry import execute_device_action
    from gpio_pins import load_gpio_pins_raw, parse_pin_id, pin_id, get_gpio_status_map

    raw = request.form.get('pin_id', '')
    action = request.form.get('action', '')
    action_map = {'on': 'resume', 'off': 'pause', 'resume': 'resume', 'pause': 'pause'}
    action_labels = {'on': 'Ein', 'off': 'Aus', 'resume': 'Resume', 'pause': 'Pause'}
    gpio_action = action_map.get(action)
    if not gpio_action:
        return _action_response("Ungültige Aktion", ok=False)
    pin = parse_pin_id(raw)
    if pin is None:
        return _action_response("Pin nicht gefunden", ok=False)
    gid = pin_id(pin)
    dev = next(
        (
            d for d in load_gpio_pins_raw()
            if parse_pin_id(d.get('pin') if d.get('pin') is not None else d.get('id')) == pin
        ),
        None,
    )
    if not dev:
        return _action_response("Pin nicht gefunden", ok=False)
    ok = execute_device_action(
        {"type": "gpio", "id": gid, "ip": gid, "pin": pin, "label": dev.get("label") or gid},
        gpio_action,
        source="manual",
        actor=_dashboard_actor(),
    )
    label = dev.get('label') or gid
    verb = action_labels.get(action, action)
    msg = f"{label}: {verb} gesendet." if ok else f"{label}: Steuerung fehlgeschlagen."
    status = None
    if ok:
        import time

        # heizstap übernimmt commands.json alle ~0.4s
        for _ in range(8):
            time.sleep(0.2)
            status = get_gpio_status_map().get(gid)
            want = "running" if gpio_action == "resume" else "paused"
            if status == want:
                break
        if not status:
            status = "running" if gpio_action == "resume" else "paused"
    return _action_response(
        msg,
        ok=ok,
        extra={
            "device_status": {
                "pin_id": gid,
                "entity_id": gid,
                "status": status,
            }
        }
        if status
        else None,
    )

@app.route('/delete_device', methods=['POST'])
def delete_device():
    from device_registry import load_ha_devices_raw, save_ha_devices

    entity_id = request.form.get('entity_id', '')
    ha_devices = [d for d in load_ha_devices_raw() if d.get('entity_id') != entity_id]
    save_ha_devices(ha_devices)
    return redirect('/')

@app.route('/update_device_note', methods=['POST'])
def update_device_note():
    from device_registry import load_ha_devices_raw, save_ha_devices

    entity_id = request.form.get('entity_id', '')
    note = request.form.get('note', '')
    ha_devices = load_ha_devices_raw()
    label = entity_id
    for dev in ha_devices:
        if dev.get('entity_id') == entity_id:
            dev['note'] = note
            label = dev.get('label') or entity_id
            break
    save_ha_devices(ha_devices)
    return _action_response(f"Notiz für {label} gespeichert.")

@app.route('/device_action', methods=['POST'])
def device_action():
    from device_registry import execute_device_action, load_ha_devices_raw

    entity_id = request.form.get('entity_id', '')
    action = request.form.get('action', '')
    action_map = {'on': 'resume', 'off': 'pause', 'resume': 'resume', 'pause': 'pause'}
    action_labels = {'on': 'Ein', 'off': 'Aus', 'resume': 'Resume', 'pause': 'Pause'}
    ha_action = action_map.get(action)
    if not ha_action:
        return _action_response("Ungültige Aktion", ok=False)
    dev = next((d for d in load_ha_devices_raw() if d.get('entity_id') == entity_id), None)
    if not dev:
        return _action_response("Gerät nicht gefunden", ok=False)
    device = {
        "type": "ha",
        "entity_id": entity_id,
        "id": entity_id,
        "label": dev.get("label") or entity_id,
    }
    ok = execute_device_action(
        device,
        ha_action,
        source="manual",
        actor=_dashboard_actor(),
    )
    label = dev.get('label') or entity_id
    verb = action_labels.get(action, action)
    if ok:
        msg = f"{label}: {verb} gesendet."
    else:
        msg = f"{label}: Steuerung fehlgeschlagen."
    status = None
    if ok:
        try:
            from device_registry import get_ha_device_status_map
            status = get_ha_device_status_map().get(entity_id)
        except Exception:
            pass
    return _action_response(
        msg,
        ok=ok,
        extra={"device_status": {"entity_id": entity_id, "status": status}} if status else None,
    )

@app.route('/save_settings', methods=['POST'])
def save_avg_minutes():
    settings = load_settings()
    settings["avg_minutes"] = int(request.form.get("avg_minutes", 45))
    save_settings(settings)

    os.makedirs(ZIELORDNER, exist_ok=True)
    with open(os.path.join(ZIELORDNER, "avg_minutes.txt"), "w") as f:
        f.write(str(settings["avg_minutes"]))

    return redirect('/smartmeter')  # â— nicht redirect(url_for('index'))!



@app.route('/delete_miner', methods=['POST'])
def delete_miner():
    ip = request.form['ip']
    try:
        from ip_recovery import remove_miner_completely

        remove_miner_completely(ip)
    except Exception:
        with open(MINER_FILE) as f:
            miners = json.load(f)
        miners = [m for m in miners if m['ip'] != ip]
        with open(MINER_FILE, 'w') as f:
            json.dump(miners, f, indent=2)
    return redirect('/')


@app.route('/api/miner/recovery/pending', methods=['GET'])
def api_recovery_pending():
    try:
        from ip_recovery import list_pending_recovery

        return jsonify(list_pending_recovery())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route('/api/miner/<path:ip>/recovery/confirm', methods=['POST'])
def api_recovery_confirm(ip):
    body = request.get_json(silent=True) or {}
    exists = bool(body.get("exists"))
    try:
        from ip_recovery import confirm_miner_exists

        ok, message = confirm_miner_exists(ip, exists=exists)
        return jsonify({"ok": ok, "message": message})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route('/update_note', methods=['POST'])
def update_note():
    ip = request.form['ip']
    note = request.form['note']
    with open(MINER_FILE) as f:
        miners = json.load(f)
    label = ip
    for miner in miners:
        if miner['ip'] == ip:
            label = miner.get('note') or miner.get('model') or ip
            miner['note'] = note
            break
    with open(MINER_FILE, 'w') as f:
        json.dump(miners, f, indent=2)
    return _action_response(f"Notiz für {label} gespeichert.")


@app.route('/telegrambot', methods=['GET', 'POST'])
def telegrambot():
    config_path = os.path.join(os.path.dirname(__file__), 'telegram_config.json')
    message = None
    config = {}

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}

    if request.method == 'POST':
        new_token = (request.form.get('telegram_token') or "").strip()
        if new_token:
            config['telegram_token'] = new_token
        config['feed_in_alarm_kw'] = request.form.get(
            'feed_in_alarm_kw', config.get('feed_in_alarm_kw', -100)
        )
        config['high_usage_alarm_kw'] = request.form.get(
            'high_usage_alarm_kw', config.get('high_usage_alarm_kw', 100)
        )
        config['power_outage_enabled'] = '1' in request.form.getlist(
            'power_outage_enabled'
        )
        config['ha_data_monitor_enabled'] = '1' in request.form.getlist(
            'ha_data_monitor_enabled'
        )
        for old_key in (
            'ev_charging_alarm_enabled',
            'ev_charging_alarm_minutes',
            'ev_charging_alarm_kw',
            'speicher_ausfall_enabled',
        ):
            config.pop(old_key, None)
        config.pop('notalarm_enabled', None)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        message = "Telegram-Einstellungen erfolgreich gespeichert."

    from telegram_ha_alarm import ha_monitor_enabled, read_ha_data_state

    ha_state = read_ha_data_state()
    ha_monitor_on = ha_monitor_enabled(config)

    return render_template(
        'telegrambot.html',
        token=config.get('telegram_token', ''),
        message=message,
        feed_in_alarm_kw=config.get('feed_in_alarm_kw', -100),
        high_usage_alarm_kw=config.get('high_usage_alarm_kw', 100),
        power_outage_enabled=bool(config.get('power_outage_enabled', False)),
        ha_data_monitor_enabled=ha_monitor_on,
        ha_connection_stable=ha_state.get('stable', False),
        ha_connection_detail=ha_state.get('detail', ''),
    )

@app.route('/automatikmodus', methods=['GET'])
def automatikmodus():
    from automation_rules import load_automation_payload

    payload = load_automation_payload(os.path.dirname(__file__))
    return render_template(
        'automatik.html',
        automation_payload=payload,
        battery_soc=load_battery_soc(),
    )

@app.route('/api/automation/config', methods=['GET', 'POST'])
def api_automation_config():
    from automation_rules import load_automation_payload, save_automation_payload

    backend_dir = os.path.dirname(__file__)
    if request.method == 'GET':
        return jsonify(load_automation_payload(backend_dir))
    try:
        body = request.get_json(silent=True) or {}
        saved = save_automation_payload(body)
        # Kein Dienst-Neustart / keine neuen Terminals: Energiemonitor lädt
        # miners.json / ha_devices.json in jedem Loop neu.
        return jsonify({
            "ok": True,
            **saved,
            "restart_scheduled": False,
            "restart_message": "Gespeichert. Regeln gelten sofort (ohne Neustart).",
        })
    except Exception as exc:
        app.logger.exception("automation save failed")
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.route('/save_automatik', methods=['POST'])
def save_automatik():
    return redirect('/automatikmodus')

@app.route('/zeitsteuerung', methods=['GET'])
def zeitsteuerung():
    from control_algorithm.config import load_config, sort_ha_devices_by_priority, sort_miners_by_priority
    from device_registry import load_gpio_pins_raw, load_ha_devices_raw

    algo_config = load_config()
    with open(MINER_FILE) as f:
        miners = sort_miners_by_priority(json.load(f), algo_config)
    ha_devices = sort_ha_devices_by_priority(load_ha_devices_raw(), algo_config)
    gpio_pins = sorted(
        load_gpio_pins_raw(),
        key=lambda p: (int(p.get("pin") or 0), str(p.get("id") or "")),
    )
    return render_template(
        'zeitsteuerung.html',
        miners=miners,
        ha_devices=ha_devices,
        gpio_pins=gpio_pins,
    )

@app.route('/save_zeitsteuerung', methods=['POST'])
def save_zeitsteuerung():
    from device_registry import (
        load_gpio_pins_raw,
        load_ha_devices_raw,
        save_gpio_pins,
        save_ha_devices,
    )
    from gpio_pins import pin_id as gpio_pin_id

    with open(MINER_FILE) as f:
        miners = json.load(f)
    for miner in miners:
        ip = miner['ip']
        miner['time_pause'] = f"time_pause_{ip}" in request.form
        miner['time_ths'] = f"time_ths_{ip}" in request.form
        for key in ['pause_time', 'resume_time', 'low_time', 'high_time']:
            miner[key] = request.form.get(f"{key}_{ip}", '')
    with open(MINER_FILE, 'w') as f:
        json.dump(miners, f, indent=2)

    ha_devices = load_ha_devices_raw()
    for dev in ha_devices:
        eid = dev['entity_id']
        dev['time_pause'] = f"time_pause_{eid}" in request.form
        for key in ['pause_time', 'resume_time']:
            dev[key] = request.form.get(f"{key}_{eid}", '')
    save_ha_devices(ha_devices)

    gpio_pins = load_gpio_pins_raw()
    for pin in gpio_pins:
        pin_no = pin.get('pin')
        if pin_no is None:
            continue
        gid = str(pin.get('id') or gpio_pin_id(int(pin_no)))
        pin['id'] = gid
        pin['time_pause'] = f"time_pause_{gid}" in request.form
        for key in ['pause_time', 'resume_time']:
            pin[key] = request.form.get(f"{key}_{gid}", '')
    save_gpio_pins(gpio_pins)
    return redirect('/zeitsteuerung')

@app.route('/miner_action', methods=['POST'])
def miner_action():
    if not session.get('logged_in'):
        return _action_response("Nicht angemeldet.", ok=False)

    from device_registry import execute_device_action, load_miners_raw

    ip = request.form.get('ip', '').strip()
    action = request.form.get('action', '').strip()
    action_labels = {
        'pause': 'Pause',
        'resume': 'Resume',
        'lowpower': 'Low Power',
        'highpower': 'High Power',
    }
    if action not in action_labels:
        return _action_response("Ungültige Aktion", ok=False)

    action_map = {
        'pause': 'pause',
        'resume': 'resume',
        'lowpower': 'low',
        'highpower': 'high',
    }
    mapped = action_map[action]
    ok = False
    msg = "Unbekannter Fehler."
    miner = None

    try:
        miner = next((m for m in load_miners_raw() if m.get('ip') == ip), None)
        if not miner:
            return _action_response("Miner nicht gefunden.", ok=False)

        device = {
            'type': 'miner',
            'ip': ip,
            'label': miner.get('note') or miner.get('model') or ip,
            'username': miner.get('username', 'root'),
            'password': miner.get('password', ''),
        }
        ok = execute_device_action(
            device,
            mapped,
            source="manual",
            actor=_dashboard_actor(),
        )
        label = miner.get('note') or miner.get('model') or ip
        verb = action_labels[action]
        if ok:
            msg = f"{label}: {verb} gesendet."
        else:
            msg = f"{label}: Steuerung fehlgeschlagen (Login/API prüfen)."
    except Exception as exc:
        app.logger.exception('miner_action fehlgeschlagen für %s', ip)
        msg = f"Steuerung fehlgeschlagen: {exc}"
        ok = False

    status = None
    if ok and miner and mapped in ('pause', 'resume'):
        try:
            from miner_control import read_miner_status
            from dashboard_miner_status import update_miner_status_entry

            status = read_miner_status(
                ip,
                miner.get('username', 'root'),
                miner.get('password', ''),
            )
            if not status:
                status = 'paused' if mapped == 'pause' else 'running'
            update_miner_status_entry(ip, status)
        except Exception:
            status = 'paused' if mapped == 'pause' else 'running'
            try:
                from dashboard_miner_status import update_miner_status_entry
                update_miner_status_entry(ip, status)
            except Exception:
                pass

    return _action_response(
        msg,
        ok=ok,
        extra={"device_status": {"ip": ip, "status": status}} if status else None,
    )

@app.route('/steuerungsalgorithmus', methods=['GET'])
def steuerungsalgorithmus():
    from control_algorithm_web import get_page_context

    ctx = get_page_context()
    devices = ctx.get('devices') or []
    return render_template(
        'steuerungsalgorithmus.html',
        config=ctx['config'],
        devices=devices,
        # Abwärtskompatibel: ältere Templates erwarten miner/ha-Listen
        miner_devices=ctx.get('miner_devices', devices),
        ha_algo_devices=ctx.get('ha_algo_devices', []),
        miners=ctx['miners'],
        ha_devices=ctx.get('ha_devices', []),
        soc_targets=ctx['soc_targets'],
        message=request.args.get('message'),
        error=request.args.get('error'),
    )


@app.route('/update_steuerungsalgorithmus', methods=['POST'])
def update_steuerungsalgorithmus():
    """Holt neuesten Steuerungsalgorithmus von GitHub, behält lokale Einstellungen."""
    from urllib.parse import quote

    from control_algorithm.updater import update_from_github

    result = update_from_github()
    if result.get('ok'):
        msg = result.get('message') or 'Steuerungsalgorithmus aktualisiert.'
        return redirect('/steuerungsalgorithmus?message=' + quote(msg))
    err = result.get('error') or 'Update fehlgeschlagen.'
    return redirect('/steuerungsalgorithmus?error=' + quote(err))


@app.route('/verbrauchsdaten-zuordnen', methods=['GET'])
def verbrauchsdaten_zuordnen():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from urllib.parse import quote
    return redirect('/?hinweis=' + quote(
        'Sensoren bitte direkt im Stromfluss-Diagramm zuordnen (auf Kästchen klicken).'
    ))


@app.route('/api/flow/control_values')
def api_flow_control_values():
    from dashboard_control import get_control_values
    return jsonify(get_control_values(os.path.dirname(__file__)))


@app.route('/save_steuerungsalgorithmus', methods=['POST'])
def save_steuerungsalgorithmus():
    from control_algorithm_web import save_from_form
    from control_algorithm.config import (
        config_fingerprint,
        invalidate_decision_board_cache,
        load_config,
        load_state,
        save_state,
    )
    from control_algorithm.calibration import maybe_reset_on_config_change

    prev_cfg = load_config()
    cfg, err = save_from_form(request.form)
    if err:
        from urllib.parse import quote
        return redirect('/steuerungsalgorithmus?error=' + quote(err))
    mode_changed = (
        bool(prev_cfg.get('enabled')) != bool(cfg.get('enabled'))
        or bool(prev_cfg.get('dry_run', True)) != bool(cfg.get('dry_run', True))
    )
    switched_to_test = bool(cfg.get('enabled')) and bool(cfg.get('dry_run')) and (
        not bool(prev_cfg.get('enabled'))
        or not bool(prev_cfg.get('dry_run', True))
    )
    state = load_state()
    state, anchor_activated = maybe_reset_on_config_change(state, cfg)
    state['config_fingerprint'] = config_fingerprint(cfg)
    # Sofort im State sichtbar – nicht erst nach dem nächsten 10-Min-Tick
    state['dry_run'] = bool(cfg.get('dry_run', True))
    invalidate_decision_board_cache(state)
    if anchor_activated:
        state.pop('forecast_cache', None)
    # Kein synchrones run_tick() hier – blockiert Flask 20+ s (Wetter + 7-Tage-Bilanz).
    # Daemon übernimmt; bei Moduswechsel (bes. Testmodus) sofort neu starten.
    daemon_msg = ''
    if cfg.get('enabled'):
        state['force_weather_refresh'] = True
        try:
            from control_algorithm.daemon_control import ensure_control_daemon

            # Testmodus / Aktiv / Aus→An: Hard-Restart → sofortiger Tick mit neuem dry_run
            need_restart = mode_changed or switched_to_test
            if ensure_control_daemon(restart=need_restart):
                if switched_to_test:
                    daemon_msg = ' Testmodus aktiv – Daemon neu gestartet (schaltet nicht).'
                elif need_restart:
                    daemon_msg = ' Steuerungs-Daemon neu gestartet.'
                else:
                    daemon_msg = ' Steuerungs-Daemon läuft.'
            else:
                daemon_msg = (
                    ' Hinweis: Steuerungs-Daemon konnte nicht gestartet werden '
                    '(Terminal „Steuerungsalgorithmus“ manuell starten).'
                )
        except Exception:
            daemon_msg = ' Hinweis: Daemon-Start fehlgeschlagen – prüfe Logs.'
    else:
        try:
            from control_algorithm.daemon_control import stop_daemon

            stop_daemon()
            daemon_msg = ' Steuerungs-Daemon gestoppt.'
        except Exception:
            pass
    save_state(state)
    from urllib.parse import quote
    msg = 'Einstellungen gespeichert.' + daemon_msg
    if anchor_activated:
        kwh = state.get('reference_anchor_kwh')
        msg += (
            f' Referenz-Anker für 24 h aktiv: bei 100 % Schönwetter '
            f'Prognose-Basis = {kwh:.0f} kWh (kWp × Sonnenstunden). '
            f'Slot-Faktoren werden in dieser Zeit nicht angepasst. '
            f'Prognose wird beim nächsten Daemon-Tick aktualisiert.'
        )
    elif cfg.get('enabled'):
        if switched_to_test or mode_changed:
            msg += ' Neuer Modus gilt sofort (Daemon-Neustart).'
        else:
            msg += ' Prognose wird beim nächsten Daemon-Tick aktualisiert (ca. 6 s).'
    return redirect('/steuerungsalgorithmus?message=' + quote(msg))


@app.route('/api/control_algorithm/runtime_locks', methods=['POST'])
def api_control_algorithm_runtime_locks():
    """Schaltet Mindestlaufzeit und An-/Aus-Bindung des Algorithmus.

    Die Sperre nach einem menschlichen Schaltbefehl bleibt unverändert.
    """
    from control_algorithm.config import load_config, save_config

    data = request.get_json(silent=True) or {}
    raw = data.get("enabled", request.form.get("enabled"))
    enabled = str(raw).strip().lower() not in ("0", "false", "off", "aus", "nein")
    cfg = load_config()
    cfg["algo_runtime_locks_enabled"] = enabled
    save_config(cfg)
    return jsonify({"ok": True, "algo_runtime_locks_enabled": enabled})


@app.route('/api/control_algorithm/night_lock', methods=['POST'])
def api_control_algorithm_night_lock():
    """Schaltet nur die erzwungenen Nachtsperren-Pausen.

    PV-Safety, Budget, SoC-Ziel und die übrigen Regeln bleiben unverändert.
    """
    from control_algorithm.config import load_config, save_config

    data = request.get_json(silent=True) or {}
    raw = data.get("enabled", request.form.get("enabled"))
    enabled = str(raw).strip().lower() not in ("0", "false", "off", "aus", "nein")
    cfg = load_config()
    cfg["night_lock_enabled"] = enabled
    save_config(cfg)
    return jsonify({"ok": True, "night_lock_enabled": enabled})


@app.route('/api/control_algorithm/status')
def api_control_algorithm_status():
    from control_algorithm_web import api_status
    return jsonify(api_status())


@app.route('/api/control_algorithm/logic_gate_layout', methods=['POST', 'DELETE'])
def api_logic_gate_layout():
    from datetime import datetime

    from control_algorithm.config import load_state, save_state
    from control_algorithm.logic_gate import LOGIC_GATE_LAYOUT_REV

    state = load_state()
    if request.method == 'DELETE':
        state.pop('logic_gate_layout', None)
        save_state(state)
        return jsonify({'ok': True, 'reset': True})

    data = request.get_json(silent=True) or {}
    positions = data.get('positions')
    edge_waypoints = data.get('edge_waypoints')
    layout_rev = str(data.get('layout_rev') or LOGIC_GATE_LAYOUT_REV)
    if not isinstance(positions, dict):
        return jsonify({'ok': False, 'error': 'positions required'}), 400

    clean = {}
    for nid, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        try:
            clean[str(nid)] = {'x': float(pos['x']), 'y': float(pos['y'])}
        except (KeyError, TypeError, ValueError):
            continue
    if not clean:
        return jsonify({'ok': False, 'error': 'no valid positions'}), 400

    clean_edges = {}
    if isinstance(edge_waypoints, dict):
        for ek, pos in edge_waypoints.items():
            if not isinstance(pos, dict):
                continue
            try:
                clean_edges[str(ek)] = {'x': float(pos['x']), 'y': float(pos['y'])}
            except (KeyError, TypeError, ValueError):
                continue

    state['logic_gate_layout'] = {
        'layout_rev': layout_rev,
        'positions': clean,
        'edge_waypoints': clean_edges,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    save_state(state)
    return jsonify({
        'ok': True,
        'count': len(clean),
        'edge_count': len(clean_edges),
        'layout_rev': layout_rev,
    })


@app.route('/api/ha/entities')
def api_ha_entities():
    from ha_entity_mapping import list_ha_entities, load_config, mapped_entity_ids, snapshot_updated_at

    # Standard: nur verfügbare Sensoren — Alt-Müll (z. B. 192.168.1.176 unavailable)
    # nicht mehr in Zuordnungs-Dropdowns. ?all=1 für Debug.
    want_all = str(request.args.get('all') or '').strip().lower() in ('1', 'true', 'yes')
    keep = mapped_entity_ids(load_config()) if not want_all else None
    entities = list_ha_entities(available_only=not want_all, keep_entity_ids=keep)
    return jsonify({
        "entities": entities,
        "updated_at": snapshot_updated_at(),
        "count": len(entities),
        "available_only": not want_all,
    })


@app.route('/api/ha/controllable_entities')
def api_ha_controllable_entities():
    from ha_entity_mapping import list_controllable_entities, snapshot_updated_at

    entities = list_controllable_entities()
    return jsonify({
        "entities": entities,
        "updated_at": snapshot_updated_at(),
        "count": len(entities),
    })


@app.route('/api/ha/device_status')
def api_ha_device_status():
    from device_registry import get_ha_device_status_map

    return jsonify(get_ha_device_status_map())


@app.route('/api/gpio/pin_status')
def api_gpio_pin_status():
    from device_registry import get_gpio_status_map

    return jsonify(get_gpio_status_map())


@app.route('/api/ha/mapping', methods=['GET', 'POST'])
def api_ha_mapping():
    from ha_entity_mapping import (
        get_visibility,
        live_values_for_mapping,
        load_config,
        parse_json_to_config,
        save_config,
    )

    if request.method == 'POST':
        if not session.get('logged_in'):
            return jsonify({"ok": False, "error": "Nicht angemeldet"}), 401
        payload = request.get_json(silent=True) or {}
        try:
            cfg = parse_json_to_config(payload)
            path = save_config(cfg)
            sync_ha_dashboard(cfg)
            return jsonify({"ok": True, "path": str(path), "config": cfg})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    cfg = load_config()
    return jsonify({
        "config": cfg,
        "live": live_values_for_mapping(cfg),
        "visibility": get_visibility(cfg),
    })


@app.route('/api/ha/visibility')
def api_ha_visibility():
    from ha_entity_mapping import get_visibility, load_config

    return jsonify(get_visibility(load_config()))


@app.route('/grundverbrauchsrechner', methods=['GET'])
def grundverbrauchsrechner():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('grundverbrauchsrechner.html')


@app.route('/api/grundverbrauch', methods=['GET'])
def api_grundverbrauch():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "error": "nicht angemeldet"}), 401
    from grundverbrauch_rechner import get_report

    return jsonify(get_report(os.path.dirname(__file__)))


@app.route('/api/grundverbrauch/annahmen', methods=['POST'])
def api_grundverbrauch_annahmen():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "error": "nicht angemeldet"}), 401
    from grundverbrauch_rechner import save_assumptions

    payload = request.get_json(silent=True) or {}
    modes = payload.get("modes") if isinstance(payload, dict) else None
    if not isinstance(modes, dict):
        return jsonify({"ok": False, "error": "modes fehlt"}), 400
    try:
        report = save_assumptions(os.path.dirname(__file__), modes)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    report["ok"] = True
    return jsonify(report)


@app.route('/statistik-konfigurieren', methods=['GET'])
def statistik_konfigurieren():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from dashboard_config import FLOW_ICONS, load_config, source_options_with_custom
    from dashboard_control import CONTROL_BINDINGS, CONTROL_ROLE_LABELS, get_control_values

    return render_template(
        'statistik_konfigurieren.html',
        dash_config=load_config(),
        source_options=source_options_with_custom(),
        flow_icons=FLOW_ICONS,
        control_bindings=CONTROL_BINDINGS,
        control_role_labels=CONTROL_ROLE_LABELS,
        control_live=get_control_values(os.path.dirname(__file__)),
        message=request.args.get('message'),
        error=request.args.get('error'),
    )


@app.route('/fehlerberichte', methods=['GET'])
def fehlerberichte():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from error_reports_ui import build_status

    return render_template('fehlerberichte.html', status=build_status())


@app.route('/api/fehlerberichte/status', methods=['GET'])
def api_fehlerberichte_status():
    if not session.get('logged_in'):
        return jsonify({"error": "unauthorized"}), 401
    from error_reports_ui import build_status

    return jsonify(build_status())


@app.route('/api/fehlerberichte/send', methods=['POST'])
def api_fehlerberichte_send():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    from error_reports_ui import run_send_now

    return jsonify(run_send_now())


@app.route('/api/dashboard/config', methods=['GET', 'POST'])
def api_dashboard_config():
    from dashboard_config import (
        load_config,
        parse_json_payload,
        save_config,
        source_options_with_custom,
    )

    if request.method == 'POST':
        if not session.get('logged_in'):
            return jsonify({"ok": False, "error": "Nicht angemeldet"}), 401
        payload = request.get_json(silent=True) or {}
        try:
            cfg = parse_json_payload(payload)
            path = save_config(cfg)
            sync_ha_dashboard()
            return jsonify({
                "ok": True,
                "path": str(path),
                "config": load_config(),
                "source_options": source_options_with_custom(),
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    cfg = load_config()
    return jsonify({
        "config": cfg,
        "source_options": source_options_with_custom(cfg),
    })


@app.route('/save_ha_mapping', methods=['POST'])
def save_ha_mapping():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from urllib.parse import quote

    from ha_entity_mapping import parse_form_to_config, save_config

    try:
        cfg = parse_form_to_config(request.form)
        path = save_config(cfg)
        sync_ha_dashboard(cfg)
    except Exception as exc:
        return redirect('/?hinweis=' + quote(f'Speichern fehlgeschlagen: {exc}'))
    return redirect('/?hinweis=' + quote(f'Sensoren gespeichert ({path.name}).'))


@app.route('/reboot', methods=['GET', 'POST'])
def reboot():
    if request.method == 'POST':
        os.system("sudo reboot")
        return "Neustart lÃ¤uft..."
    return render_template('reboot.html')



def public_update_status() -> dict:
    try:
        from public_update import dashboard_status

        return dashboard_status()
    except Exception:
        return {
            "upload_allowed": False,
            "uploading": False,
            "downloading": False,
            "local_version": 0,
            "remote_version": None,
            "phase": "",
            "message": "Update-Status nicht verfügbar.",
            "repo_url": "",
        }


@app.route('/api/public-update/status')
def api_public_update_status():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "message": "Nicht angemeldet"}), 401
    return jsonify({"ok": True, **public_update_status()})


@app.route('/api/public-update/upload', methods=['POST'])
def api_public_update_upload():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "message": "Nicht angemeldet"}), 401
    from public_update import start_upload

    ok, message = start_upload()
    return jsonify({"ok": ok, "message": message, **public_update_status()}), (200 if ok else 409)


@app.route('/api/public-update/download', methods=['POST'])
def api_public_update_download():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "message": "Nicht angemeldet"}), 401
    from public_update import start_download

    ok, message = start_download()
    return jsonify({"ok": ok, "message": message, **public_update_status()}), (200 if ok else 409)



@app.route('/dienste')
def dienste():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    from service_control import all_status
    return render_template('dienste.html', services=all_status())


@app.route('/api/services/status')
def api_services_status():
    if not session.get('logged_in'):
        return jsonify({"error": "unauthorized"}), 401
    from service_control import all_status
    return jsonify({"services": all_status()})


@app.route('/api/services/start/<service_id>', methods=['POST'])
def api_service_start(service_id):
    if not session.get('logged_in'):
        return jsonify({"ok": False, "message": "Nicht angemeldet"}), 401
    from service_control import start_homeassistant, start_miner, start_tailscale
    starters = {
        "miner": start_miner,
        "homeassistant": start_homeassistant,
        "tailscale": start_tailscale,
    }
    starter = starters.get(service_id)
    if not starter:
        return jsonify({"ok": False, "message": "Unbekannter Dienst"}), 404
    ok, message = starter()
    return jsonify({"ok": ok, "message": message})


@app.route('/api/services/start-all', methods=['POST'])
def api_services_start_all():
    if not session.get('logged_in'):
        return jsonify({"ok": False, "message": "Nicht angemeldet"}), 401
    from service_control import start_all
    results = start_all()
    return jsonify({"ok": all(r["ok"] for r in results), "results": results})

EM_DIR = os.path.join(os.path.dirname(__file__), "energy_monitor")


def _read_em_file(filename, default="n/a"):
    """Liest Energy-Monitor-Datei; bei MINER_RAM_CACHE zuerst /dev/shm."""
    base = os.path.dirname(__file__)
    try:
        from miner_flow_data import _read_em_text

        v = _read_em_text(base, filename)
        return v if v is not None else default
    except Exception:
        path = os.path.join(EM_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
                return value if value else default
        except Exception:
            return default


@app.route('/energy_data')
def energy_data():
    bat = load_battery_data()
    computed = compute_aktueller_verbrauch_kw(
        os.path.dirname(__file__), bat.get("power_kw")
    )
    return jsonify({
        "current": computed or "n/a",
        "grid": _read_em_file("energy_value.txt"),
        "avg": _read_em_file("energy_avg.txt"),
    })


from energy_stats_24h import get_stats_24h, accumulate_from_flow as accumulate_24h
from energy_stats_30d import get_stats_30d, accumulate_from_flow as accumulate_30d
from energy_stats_year import get_stats_year, accumulate_from_flow as accumulate_year
from energy_monthly_peak_15min import get_monthly_peak, update_monthly_peak
from energy_timeseries_24h import get_timeseries_24h, maybe_record_sample
import time


@app.route('/flow_data')
def flow_data():
    bat = load_battery_data()
    flow = build_flow_response(
        os.path.dirname(__file__),
        load_battery_soc,
        bat.get("power_kw"),
    )
    flow["battery_soc"] = bat.get("soc") or flow.get("battery_soc")
    flow["battery_mode"] = bat.get("mode")
    flow["battery_display_kw"] = bat.get("display_kw")
    flow["battery_power_kw"] = bat.get("power_kw")
    return jsonify(flow)


@app.route('/battery_data')
def battery_data():
    return jsonify(load_battery_data())


@app.route('/miner_status')
def miner_status():
    details = load_miner_status_details()
    payload = {ip: info.get("status") for ip, info in details.items()}
    payload["_details"] = details
    return jsonify(payload)


@app.route('/hashrate_timeseries_24h')
def hashrate_timeseries_24h():
    from hashrate_timeseries import get_hashrate_timeseries

    return jsonify(get_hashrate_timeseries(os.path.dirname(__file__)))


@app.route('/energy_stats_24h')
def energy_stats_24h():
    return jsonify(get_stats_24h(os.path.dirname(__file__)))


@app.route('/energy_timeseries_24h')
def energy_timeseries_24h():
    return jsonify(get_timeseries_24h(os.path.dirname(__file__)))


@app.route('/energy_stats_30d')
def energy_stats_30d():
    return jsonify(get_stats_30d(os.path.dirname(__file__)))


@app.route('/energy_stats_year')
def energy_stats_year():
    return jsonify(get_stats_year(os.path.dirname(__file__)))


@app.route('/monthly_peak_15min')
def monthly_peak_15min():
    return jsonify(get_monthly_peak(os.path.dirname(__file__)))


HA_SYNC_INTERVAL_SEC = 30


def sync_ha_dashboard(cfg=None):
    """HA-Snapshot → energy_monitor/*.txt für Dashboard und Statistik."""
    from ha_entity_mapping import load_config
    from energy_monitor.ha_live_reader import write_dashboard_files

    base = os.path.dirname(__file__)
    cfg = cfg or load_config()
    write_dashboard_files(cfg)
    accumulate_24h(base)
    # Stündlich Live-Werte auf SD spiegeln → nach Stromausfall kein Monat-alter Fallback
    try:
        from ram_cache import maybe_flush_timeseries

        maybe_flush_timeseries(base)
    except Exception:
        pass


_grundverbrauch_tick_at = 0.0


def _maybe_tick_grundverbrauch(base: str) -> None:
    global _grundverbrauch_tick_at
    now = time.time()
    if now - _grundverbrauch_tick_at < 900:
        return
    _grundverbrauch_tick_at = now
    try:
        from grundverbrauch_rechner import tick as tick_grundverbrauch

        tick_grundverbrauch(base)
    except Exception:
        pass


def _energy_stats_loop():
    base = os.path.dirname(__file__)
    while True:
        try:
            update_monthly_peak(base)
            maybe_record_sample(base)
            _maybe_tick_grundverbrauch(base)
        except Exception:
            pass
        time.sleep(5)


def _ha_dashboard_loop():
    import traceback

    while True:
        try:
            sync_ha_dashboard()
        except Exception as exc:
            # Nicht still schlucken: nach Reboot sonst stundenlang tote Dashboard-Daten
            print(f"[HA-Sync] Fehler: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        time.sleep(HA_SYNC_INTERVAL_SEC)


_stats_thread_started = False
_ha_sync_thread_started = False


def start_energy_stats_thread():
    global _stats_thread_started
    if _stats_thread_started:
        return
    _stats_thread_started = True
    threading.Thread(target=_energy_stats_loop, daemon=True).start()


def start_ha_dashboard_thread():
    global _ha_sync_thread_started
    if _ha_sync_thread_started:
        return
    _ha_sync_thread_started = True
    threading.Thread(target=_ha_dashboard_loop, daemon=True).start()


start_energy_stats_thread()
start_ha_dashboard_thread()


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


TELEGRAM_CONFIG_FILE = "telegram_config.json"


def save_telegram_token(token):
    config_path = os.path.join(os.path.dirname(__file__), TELEGRAM_CONFIG_FILE)
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
    config["telegram_token"] = token
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def load_telegram_token():
    if os.path.exists(TELEGRAM_CONFIG_FILE):
        with open(TELEGRAM_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("telegram_token", "")
    return ""


def open_browser():
    if os.environ.get("MINER_OPEN_BROWSER") != "1":
        return
    subprocess.Popen(["xdg-open", f"http://{get_ip()}:5000"])


if __name__ == "__main__":
    start_energy_stats_thread()
    start_ha_dashboard_thread()
    if os.environ.get("MINER_OPEN_BROWSER") == "1":
        threading.Timer(1.0, open_browser).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
