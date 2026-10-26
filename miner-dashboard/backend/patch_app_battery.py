#!/usr/bin/env python3
"""app.py: Batterie-SOC + battery_data Route; index übergibt battery_soc."""
from pathlib import Path
import re
import shutil
from datetime import datetime

APP = Path.home() / "miner-dashboard/backend/app.py"

IMPORT_LINE = "from dashboard_battery import load_battery_soc\n"

BATTERY_ROUTE = '''

@app.route('/battery_data')
def battery_data():
    soc = load_battery_soc()
    return jsonify({"soc": soc})
'''

INDEX_OLD = """    return render_template('dashboard.html', miners=miners, settings=settings)"""

INDEX_NEW = """    battery_soc = load_battery_soc()
    return render_template('dashboard.html', miners=miners, settings=settings, battery_soc=battery_soc)"""


def main():
    if not APP.exists():
        raise SystemExit(f"Nicht gefunden: {APP}")

    text = APP.read_text(encoding="utf-8")
    backup = APP.with_name(f"app.py.bak-battery-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    if "from dashboard_battery import load_battery_soc" not in text:
        if "from flask import" in text:
            text = text.replace(
                "from flask import",
                IMPORT_LINE + "from flask import",
                1,
            )
        else:
            text = IMPORT_LINE + text

    if "def battery_data" not in text:
        marker = "\nif __name__ == '__main__':"
        if marker not in text:
            marker = '\nif __name__ == "__main__":'
        if marker in text:
            text = text.replace(marker, BATTERY_ROUTE + marker, 1)
        else:
            text = text.rstrip() + BATTERY_ROUTE

    if INDEX_NEW not in text and INDEX_OLD in text:
        text = text.replace(INDEX_OLD, INDEX_NEW, 1)

    text = re.sub(
        r"try:\s*\n\s*soc_fn = load_battery_soc\s*\n\s*except NameError:",
        "soc_fn = load_battery_soc",
        text,
    )

    APP.write_text(text, encoding="utf-8")
    print(f"OK – Batterie in app.py. Backup: {backup.name}")


if __name__ == "__main__":
    main()
