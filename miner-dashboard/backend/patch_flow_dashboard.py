#!/usr/bin/env python3
"""app.py: /flow_data nutzt miner_flow_data + load_battery_soc."""
from pathlib import Path
import re
import shutil
from datetime import datetime

APP = Path.home() / "miner-dashboard/backend/app.py"

ROUTE = '''
from miner_flow_data import build_flow_response


@app.route('/flow_data')
def flow_data():
    return jsonify(build_flow_response(os.path.dirname(__file__), load_battery_soc))
'''


def main():
    if not APP.exists():
        raise SystemExit(f"Nicht gefunden: {APP}")

    text = APP.read_text(encoding="utf-8")
    if "from miner_flow_data import build_flow_response" in text:
        print("flow_data bereits mit miner_flow_data – OK.")
        return

    backup = APP.with_name(f"app.py.bak-flow-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    if "def load_battery_soc" not in text:
        print("WARNUNG: load_battery_soc fehlt – Batterie evtl. n/a.")

    text = re.sub(
        r"\n@app\.route\('/flow_data'\)\ndef flow_data\(\):.*?\n    \}\)\n",
        "\n",
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\ndef _read_enline_json_field\(field, fallback=.*?\n    return fallback\n",
        "\n",
        text,
        count=1,
        flags=re.DOTALL,
    )

    marker = "\nif __name__ == '__main__':"
    if marker not in text:
        marker = '\nif __name__ == "__main__":'
    if marker not in text:
        text = text.rstrip() + "\n" + ROUTE.strip() + "\n"
    else:
        text = text.replace(marker, ROUTE + marker, 1)

    APP.write_text(text, encoding="utf-8")
    print(f"app.py flow_data gepatcht. Backup: {backup}")


if __name__ == "__main__":
    main()
