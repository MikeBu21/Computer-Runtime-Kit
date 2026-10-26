#!/usr/bin/env python3
from pathlib import Path
import shutil
from datetime import datetime
import re


APP = Path.home() / "miner-dashboard/backend/app.py"


def replace_once(text, old, new):
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def main():
    if not APP.exists():
        raise SystemExit(f"app.py nicht gefunden: {APP}")

    text = APP.read_text(encoding="utf-8")
    original = text

    backup = APP.with_name(f"app.py.bak-automatik-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    if "DATEN_FILE" not in text:
        text, ok = replace_once(
            text,
            'ZIELORDNER = os.path.join(os.path.dirname(__file__), "shared_settings")\n',
            'ZIELORDNER = os.path.join(os.path.dirname(__file__), "shared_settings")\n'
            'DATEN_FILE = os.path.join(os.path.dirname(__file__), "daten.json")\n',
        )
        if not ok:
            raise SystemExit("Konnte ZIELORDNER-Zeile nicht finden.")

    helper = '''

def load_battery_soc():
    """Aktuellen Batterie-SOC aus daten.json (scraper.py) lesen."""
    base_dir = os.path.dirname(__file__)
    project_dir = os.path.abspath(os.path.join(base_dir, ".."))
    home_dir = os.path.expanduser("~")
    daten_files = [
        os.path.join(home_dir, "daten.json"),
        DATEN_FILE,
        os.path.join(base_dir, "energy_monitor", "daten.json"),
        os.path.join(base_dir, "shared_settings", "daten.json"),
        os.path.join(project_dir, "daten.json"),
    ]

    # Falls scraper.py die Datei in einem anderen Unterordner ablegt,
    # suchen wir im ganzen miner-dashboard-Projekt nach daten.json.
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git")]
        if "daten.json" in files:
            path = os.path.join(root, "daten.json")
            if path not in daten_files:
                daten_files.append(path)

    existing_file = next((path for path in daten_files if os.path.exists(path)), None)
    if existing_file is None:
        return None
    try:
        with open(existing_file, "r") as f:
            data = json.load(f)

        def find_soc(value, key_hint=""):
            if isinstance(value, dict):
                preferred = (
                    "soc", "SoC", "SOC", "battery_soc", "battery", "batterie",
                    "value", "wert", "percent", "percentage", "prozent",
                    "ladestand", "ladezustand", "speicher", "akku", "capacity"
                )
                for key in preferred:
                    if key in value and value[key] not in (None, ""):
                        found = find_soc(value[key], key)
                        if found is not None:
                            return found
                if len(value) == 1:
                    only_key, only_value = next(iter(value.items()))
                    return find_soc(only_value, str(only_key))
                for key, nested in value.items():
                    found = find_soc(nested, str(key))
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = find_soc(item, key_hint)
                    if found is not None:
                        return found
            elif value not in (None, ""):
                hint = key_hint.lower()
                text_value = str(value).strip().replace("%", "").replace(",", ".")
                if any(word in hint for word in ("soc", "battery", "batterie", "lade", "percent", "prozent", "value", "wert", "speicher", "akku", "capacity")):
                    return text_value
                try:
                    number = float(text_value)
                    if 0 <= number <= 100:
                        return str(int(number) if number.is_integer() else number)
                except Exception:
                    pass
            return None

        if isinstance(data, dict):
            for key in ("soc", "SoC", "SOC", "battery_soc", "battery", "batterie", "value", "wert", "percent", "percentage", "ladestand", "ladezustand", "speicher", "akku", "capacity"):
                if key in data and data[key] not in (None, ""):
                    found = find_soc(data[key], key)
                    return found if found is not None else str(data[key])
        return find_soc(data)
    except Exception:
        return None
'''

    if "def load_battery_soc" in text:
        text = re.sub(
            r"\ndef load_battery_soc\(\):\n(?=\s{4})(?:(?!\ndef\s|\n@app\.route).)*",
            helper,
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        marker = "\ndef load_settings():\n"
        if marker not in text:
            raise SystemExit("Konnte def load_settings() nicht finden.")
        text = text.replace(marker, helper + marker, 1)

    if "battery_soc=battery_soc" not in text:
        route_pattern = re.compile(
            r"(def automatikmodus\(\):\n(?:(?!\n@app\.route|\ndef ).)*?)"
            r"(\n\s*)return render_template\((['\"])\s*automatik\.html\s*\3,\s*miners=miners\)",
            re.DOTALL,
        )

        def add_battery_to_route(match):
            body = match.group(1)
            indent = match.group(2)
            quote = match.group(3)
            if "battery_soc = load_battery_soc()" not in body:
                body = body + f"{indent}battery_soc = load_battery_soc()"
            return (
                body
                + f"{indent}return render_template({quote}automatik.html{quote}, "
                + "miners=miners, battery_soc=battery_soc)"
            )

        text, count = route_pattern.subn(add_battery_to_route, text, count=1)
        if count == 0:
            raise SystemExit("Konnte automatikmodus()-Route nicht finden.")

    if "dashboard.html', miners=miners, settings=settings, battery_soc=battery_soc" not in text and '"dashboard.html", miners=miners, settings=settings, battery_soc=battery_soc' not in text:
        index_pattern = re.compile(
            r"(def index\(\):\n(?:(?!\n@app\.route|\ndef ).)*?)"
            r"(\n\s*)return render_template\((['\"])\s*dashboard\.html\s*\3,\s*miners=miners,\s*settings=settings\)",
            re.DOTALL,
        )

        def add_battery_to_index(match):
            body = match.group(1)
            indent = match.group(2)
            quote = match.group(3)
            if "battery_soc = load_battery_soc()" not in body:
                body = body + f"{indent}battery_soc = load_battery_soc()"
            return (
                body
                + f"{indent}return render_template({quote}dashboard.html{quote}, "
                + "miners=miners, settings=settings, battery_soc=battery_soc)"
            )

        text, count = index_pattern.subn(add_battery_to_index, text, count=1)
        if count == 0:
            raise SystemExit("Konnte index()-Dashboard-Route nicht finden.")

    if "auto_battery" not in text:
        lines = text.splitlines(keepends=True)
        in_save_automatik = False
        inserted = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def save_automatik("):
                in_save_automatik = True
                continue
            if in_save_automatik and stripped.startswith("@app.route"):
                break
            if in_save_automatik and "miner[" in line and "auto" in line and "request.form" in line:
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(index + 1, f'{indent}miner[\'auto_battery\'] = f"battery_auto_{{ip}}" in request.form\n')
                inserted = True
                break
        if not inserted:
            raise SystemExit("Konnte auto-Zeile in save_automatik() nicht finden.")
        text = "".join(lines)

    if "soc_resume" not in text or "soc_pause" not in text:
        lines = text.splitlines(keepends=True)
        in_save_automatik = False
        inserted = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def save_automatik("):
                in_save_automatik = True
                continue
            if in_save_automatik and stripped.startswith("@app.route"):
                break
            if in_save_automatik and "miner[key]" in line and "request.form.get" in line:
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(
                    index + 1,
                    f"{indent}miner['soc_resume'] = miner.get('lower_kw', '')\n"
                    f"{indent}miner['soc_pause'] = miner.get('raise_kw', '')\n",
                )
                inserted = True
                break
        if inserted:
            text = "".join(lines)

    if "def battery_data" not in text:
        route = '''

@app.route('/battery_data')
def battery_data():
    return jsonify({"soc": load_battery_soc()})
'''
        marker = "\nif __name__ == '__main__':"
        if marker in text:
            text = text.replace(marker, route + marker, 1)
        else:
            text += route

    if "def flow_data" not in text:
        route = '''

def _read_first_existing_value(filenames, fallback="n/a"):
    base_dir = os.path.dirname(__file__)
    search_dirs = [
        base_dir,
        os.path.join(base_dir, "shared_settings"),
        os.path.join(base_dir, "energy_monitor"),
        os.path.abspath(os.path.join(base_dir, "..")),
    ]
    for directory in search_dirs:
        for filename in filenames:
            path = os.path.join(directory, filename)
            try:
                with open(path, "r") as f:
                    value = f.read().strip()
                if value:
                    return value.replace(" kW", "").replace("kw", "").strip()
            except Exception:
                pass
    return fallback


def _read_enline_json_field(field, fallback="n/a"):
    base_dir = os.path.dirname(__file__)
    search_dirs = [
        base_dir,
        os.path.join(base_dir, "shared_settings"),
        os.path.join(base_dir, "energy_monitor"),
        os.path.abspath(os.path.join(base_dir, "..")),
    ]
    for directory in search_dirs:
        path = os.path.join(directory, "enline_values.json")
        try:
            with open(path, "r") as f:
                data = json.load(f)
            values = data.get("values_kw") or data
            if field in values and values[field] not in (None, ""):
                return str(values[field])
        except Exception:
            pass
    return fallback


@app.route('/flow_data')
def flow_data():
    grid = _read_first_existing_value(["energy_value.txt"])
    hydro = _read_first_existing_value([
        "wasserkraft_value.txt",
        "wasserkraftwerk_value.txt",
        "wasser_value.txt",
        "hydro_value.txt",
        "hydro_kw.txt",
    ])
    pv = _read_first_existing_value([
        "pv_value.txt",
        "pv_kw.txt",
        "solar_value.txt",
        "solar_kw.txt",
        "pv_gesamt.txt",
    ])
    haus = _read_first_existing_value([
        "wohnhaus_value.txt",
        "wohnhaus_erzeugung_value.txt",
        "haus_value.txt",
    ])
    betrieb = _read_first_existing_value([
        "betrieb_value.txt",
        "betrieb_kw.txt",
        "company_value.txt",
    ])
    erzeugung_gesamt = _read_first_existing_value([
        "erzeugung_gesamt_value.txt",
    ])
    if haus == "n/a":
        haus = _read_enline_json_field("wohnhaus")
    if betrieb == "n/a":
        betrieb = _read_enline_json_field("betrieb")
    if erzeugung_gesamt == "n/a":
        erzeugung_gesamt = _read_enline_json_field("erzeugung_gesamt")
    return jsonify({
        "grid": grid,
        "hydro": hydro,
        "pv": pv,
        "haus": haus,
        "betrieb": betrieb,
        "erzeugung_gesamt": erzeugung_gesamt,
        "battery_soc": load_battery_soc(),
    })
'''
        marker = "\nif __name__ == '__main__':"
        if marker in text:
            text = text.replace(marker, route + marker, 1)
        else:
            text += route

    if text == original:
        print("Keine Änderung nötig. app.py war bereits angepasst.")
    else:
        APP.write_text(text, encoding="utf-8")
        print("app.py wurde minimal angepasst.")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
