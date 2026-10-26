#!/usr/bin/env python3
"""Bestehende flow_data-Route in app.py um Enline-Felder erweitern."""
from pathlib import Path
import re
import shutil
from datetime import datetime


APP = Path.home() / "miner-dashboard/backend/app.py"

FLOW_BLOCK = '''
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


def main():
    if not APP.exists():
        raise SystemExit(f"app.py nicht gefunden: {APP}")

    text = APP.read_text(encoding="utf-8")
    if '"haus"' in text and '"betrieb"' in text and "_read_enline_json_field" in text:
        print("flow_data war bereits erweitert.")
        return

    backup = APP.with_name(f"app.py.bak-flow-enline-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    if "def flow_data" not in text:
        marker = "\nif __name__ == '__main__':"
        if marker not in text:
            raise SystemExit("Weder flow_data noch __main__ gefunden.")
        text = text.replace(marker, FLOW_BLOCK + marker, 1)
    else:
        text = re.sub(
            r"\ndef _read_enline_json_field\(field, fallback=.*?\n@app\.route\('/flow_data'\)\ndef flow_data\(\):.*?\n    \}\)\n",
            "\n" + FLOW_BLOCK.strip() + "\n",
            text,
            count=1,
            flags=re.DOTALL,
        )
        if '"haus"' not in text:
            text = re.sub(
                r"@app\.route\('/flow_data'\)\ndef flow_data\(\):.*?\n    \}\)\n",
                FLOW_BLOCK.strip() + "\n",
                text,
                count=1,
                flags=re.DOTALL,
            )

    APP.write_text(text, encoding="utf-8")
    print(f"app.py flow_data erweitert. Backup: {backup}")


if __name__ == "__main__":
    main()
