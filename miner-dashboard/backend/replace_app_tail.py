#!/usr/bin/env python3
"""
Repariert app.py: alles ab /energy_data wird sauber ersetzt (IndentationError weg).
Behält Login, Miner, Automatik usw. oben.

Pi: python3 replace_app_tail.py
"""
from __future__ import annotations

import py_compile
import re
import shutil
from datetime import datetime
from pathlib import Path

APP = Path.home() / "miner-dashboard/backend/app.py"

CLEAN_TAIL = '''
EM_DIR = os.path.join(os.path.dirname(__file__), "energy_monitor")


def _read_em_file(filename, default="n/a"):
    path = os.path.join(EM_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
            return value if value else default
    except Exception:
        return default


@app.route('/energy_data')
def energy_data():
    return jsonify({
        "current": _read_em_file("energy_value.txt"),
        "avg": _read_em_file("energy_avg.txt"),
    })


from miner_flow_data import build_flow_response
from dashboard_battery import load_battery_soc


@app.route('/flow_data')
def flow_data():
    return jsonify(build_flow_response(os.path.dirname(__file__), load_battery_soc))


@app.route('/battery_data')
def battery_data():
    return jsonify({"soc": load_battery_soc()})


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
    with open(TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"telegram_token": token}, f, indent=4)


def load_telegram_token():
    if os.path.exists(TELEGRAM_CONFIG_FILE):
        with open(TELEGRAM_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("telegram_token", "")
    return ""


def open_browser():
    subprocess.Popen(["xdg-open", f"http://{get_ip()}:5000"])


if __name__ == "__main__":
    threading.Timer(1.0, open_browser).start()
    app.run(host="0.0.0.0", port=5000)
'''


def _fix_header(text: str) -> str:
    text = text.replace("Flask(_name_)", "Flask(__name__)")
    text = text.replace("dirname(_file_)", "dirname(__file__)")
    if "from dashboard_battery import load_battery_soc" not in text:
        text = text.replace(
            "from flask import",
            "from dashboard_battery import load_battery_soc\nfrom flask import",
            1,
        )
    return text


def _fix_index(text: str) -> str:
    pattern = (
        r"(@app\.route\('/'\)\s*\ndef index\(\):.*?)"
        r"return render_template\('dashboard\.html',\s*miners=miners,\s*settings=settings"
        r"(?:,\s*battery_soc=battery_soc)?\)"
    )

    def repl(m: re.Match) -> str:
        return (
            m.group(1)
            + "battery_soc = load_battery_soc()\n    "
            + "return render_template('dashboard.html', miners=miners, settings=settings, battery_soc=battery_soc)"
        )

    new_text, n = re.subn(pattern, repl, text, count=1, flags=re.DOTALL)
    return new_text if n else text


def _fix_automatik(text: str) -> str:
    old = (
        "@app.route('/automatikmodus', methods=['GET'])\n"
        "def automatikmodus():\n"
        "    with open(MINER_FILE) as f:\n"
        "        miners = json.load(f)\n"
        "    return render_template('automatik.html', miners=miners)"
    )
    new = (
        "@app.route('/automatikmodus', methods=['GET'])\n"
        "def automatikmodus():\n"
        "    with open(MINER_FILE) as f:\n"
        "        miners = json.load(f)\n"
        "    return render_template('automatik.html', miners=miners, battery_soc=load_battery_soc())"
    )
    if old in text and "battery_soc=load_battery_soc" not in text.split("automatikmodus")[1][:400]:
        text = text.replace(old, new, 1)
    return text


def _fix_save_automatik(text: str) -> str:
    needle = 'miner[\'auto\'] = f"auto_{ip}" in request.form'
    insert = (
        needle + "\n        miner['auto_battery'] = f\"battery_auto_{ip}\" in request.form"
    )
    if needle in text and "auto_battery" not in text:
        text = text.replace(needle, insert, 1)
    return text


def _cut_before_energy_data(text: str) -> str:
    m = re.search(r"\n@app\.route\('/energy_data'\)", text)
    if not m:
        m = re.search(r"\n@app\.route\(\"/energy_data\"\)", text)
    if not m:
        raise SystemExit("energy_data Route nicht gefunden – app.py Struktur unbekannt.")
    return text[: m.start()] + "\n"


def _remove_junk_lines(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s in ("soc_fn = None", "soc_fn = load_battery_soc"):
            continue
        if s.startswith("from miner_flow_data import"):
            continue
        if s.startswith("def _read_enline_json_field"):
            continue
        if s.startswith("def _read_first_existing_value"):
            continue
        out.append(line)
    return "\n".join(out)


def main():
    if not APP.exists():
        raise SystemExit(f"Nicht gefunden: {APP}")

    raw = APP.read_text(encoding="utf-8")
    backup = APP.with_name(f"app.py.bak-tail-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    head = _cut_before_energy_data(raw)
    head = _remove_junk_lines(head)
    head = _fix_header(head)
    head = _fix_index(head)
    head = _fix_automatik(head)
    head = _fix_save_automatik(head)

    final = head.rstrip() + "\n" + CLEAN_TAIL

    try:
        py_compile.compile(APP, doraise=True)
    except Exception:
        pass

    with open(APP.with_suffix(".py.check"), "w", encoding="utf-8") as f:
        f.write(final)
    py_compile.compile(str(APP.with_suffix(".py.check")), doraise=True)
    APP.with_suffix(".py.check").unlink(missing_ok=True)

    APP.write_text(final, encoding="utf-8")
    print(f"OK – app.py Tail ersetzt. Backup: {backup.name}")
    print("Test: source venv/bin/activate && python -m py_compile app.py && python app.py")


if __name__ == "__main__":
    main()
