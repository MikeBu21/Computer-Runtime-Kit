#!/usr/bin/env python3
"""
app.py reparieren (IndentationError, flow_data, Batterie).
Auf dem Pi: python3 fix_app_py_syntax.py
"""
from __future__ import annotations

import py_compile
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

APP = Path.home() / "miner-dashboard/backend/app.py"

LOAD_SETTINGS_BLOCK = '''def load_settings():
    settings = {
        "avg_minutes": 45,
        "current_kw": "n/a",
        "avg_kw": "n/a",
        "smartmeter_url": "",
        "login_username": "miningfarm",
        "login_password": "Beispiel",
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
'''

FLOW_BLOCK = '''
from miner_flow_data import build_flow_response


@app.route('/flow_data')
def flow_data():
    return jsonify(build_flow_response(os.path.dirname(__file__), load_battery_soc))


@app.route('/battery_data')
def battery_data():
    return jsonify({"soc": load_battery_soc()})
'''


def compiles(text: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(text)
            path = tmp.name
        py_compile.compile(path, doraise=True)
        Path(path).unlink(missing_ok=True)
        return True
    except py_compile.PyCompileError:
        return False


def _fix_basics(text: str) -> str:
    text = text.replace("Flask(_name_)", "Flask(__name__)")
    text = text.replace("dirname(_file_)", "dirname(__file__)")
    text = text.replace("if _name_ == '_main_':", "if __name__ == '__main__':")
    text = text.replace('if _name_ == "_main_":', 'if __name__ == "__main__":')
    return text


def _remove_soc_junk(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s in ("soc_fn = None", "soc_fn = load_battery_soc"):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _fix_load_settings(text: str) -> str:
    if re.search(r"def load_settings\(\):.*?return settings", text, re.DOTALL):
        text = re.sub(
            r"def load_settings\(\):.*?return settings\n",
            LOAD_SETTINGS_BLOCK + "\n",
            text,
            count=1,
            flags=re.DOTALL,
        )
    return text


def _fix_index(text: str) -> str:
    if "battery_soc = load_battery_soc()" in text:
        return text

    pattern = (
        r"(def index\(\):.*?)"
        r"return render_template\('dashboard\.html', miners=miners, settings=settings\)"
    )

    def repl(match: re.Match) -> str:
        body = match.group(1)
        return (
            body
            + "    battery_soc = load_battery_soc()\n"
            + "    return render_template('dashboard.html', miners=miners, settings=settings, battery_soc=battery_soc)"
        )

    new_text, n = re.subn(pattern, repl, text, count=1, flags=re.DOTALL)
    return new_text if n else text


def _ensure_imports(text: str) -> str:
    if "from dashboard_battery import load_battery_soc" not in text:
        if "from flask import" in text:
            text = text.replace(
                "from flask import",
                "from dashboard_battery import load_battery_soc\nfrom flask import",
                1,
            )
        else:
            text = "from dashboard_battery import load_battery_soc\n" + text
    return text


def _ensure_flow_routes(text: str) -> str:
    text = re.sub(
        r"\nfrom miner_flow_data import build_flow_response\s*",
        "\n",
        text,
    )
    text = re.sub(
        r"\n@app\.route\('/flow_data'\)\s*\ndef flow_data\(\):.*?\n    \}\)\s*",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\n@app\.route\('/battery_data'\)\s*\ndef battery_data\(\):.*?\n    \}\)\s*",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\ndef _read_enline_json_field\(.*?\n    return fallback\s*\n",
        "\n",
        text,
        flags=re.DOTALL,
    )

    if "def flow_data" not in text:
        for marker in ("\nif __name__ == '__main__':", '\nif __name__ == "__main__":', "\ndef open_browser():"):
            if marker in text:
                text = text.replace(marker, FLOW_BLOCK + marker, 1)
                break
        else:
            text = text.rstrip() + "\n" + FLOW_BLOCK
    return text


def _restore_from_backup() -> Optional[str]:
    backups = sorted(APP.parent.glob("app.py.bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for bak in backups:
        if "syntax" in bak.name:
            continue
        try:
            text = bak.read_text(encoding="utf-8")
        except Exception:
            continue
        if "def load_settings" in text and compiles(_fix_basics(text)):
            return text
    return None


def repair(text: str) -> str:
    text = _fix_basics(text)
    text = _remove_soc_junk(text)
    text = _fix_load_settings(text)
    text = _ensure_imports(text)
    text = _fix_index(text)
    text = _ensure_flow_routes(text)
    return text


def main():
    if not APP.exists():
        raise SystemExit(f"Nicht gefunden: {APP}")

    original = APP.read_text(encoding="utf-8")
    backup = APP.with_name(f"app.py.bak-syntax-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)

    candidates = [repair(original)]
    restored = _restore_from_backup()
    if restored:
        candidates.append(repair(restored))

    for text in candidates:
        if compiles(text):
            APP.write_text(text, encoding="utf-8")
            print(f"OK – app.py repariert. Backup: {backup.name}")
            return

    APP.write_text(candidates[0], encoding="utf-8")
    print("WARNUNG: Syntax evtl. noch fehlerhaft – bitte Ausgabe von py_compile senden.")
    print(f"Backup: {backup.name}")


if __name__ == "__main__":
    main()
