#!/usr/bin/env python3
"""
app.py auf dem Pi reparieren:
- _name_ / _file_ Tippfehler (falsche Downloads-Kopie)
- optional aus Backup wiederherstellen
- auto_battery in save_automatik
"""
from datetime import datetime
from pathlib import Path
from typing import Optional
import shutil

APP = Path.home() / "miner-dashboard/backend/app.py"


def newest_backup() -> Optional[Path]:
    backups = sorted(APP.parent.glob("app.py.bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backups[0] if backups else None


def is_broken(text: str) -> bool:
    return "Flask(_name_)" in text or "dirname(_file_)" in text or "if _name_ ==" in text


def fix_typos(text: str) -> str:
    text = text.replace("Flask(_name_)", "Flask(__name__)")
    text = text.replace("dirname(_file_)", "dirname(__file__)")
    text = text.replace("if _name_ == '_main_':", "if __name__ == '__main__':")
    text = text.replace("if _name_ == \"_main_\":", 'if __name__ == "__main__":')
    return text


def add_auto_battery(text: str) -> str:
    if "auto_battery" in text and "battery_auto_" in text:
        return text
    needle = "miner['auto'] = f\"auto_{ip}\" in request.form"
    if needle not in text:
        needle = 'miner["auto"] = f"auto_{ip}" in request.form'
    if needle not in text:
        raise SystemExit(
            "save_automatik nicht gefunden – bitte manuell einfügen:\n"
            "  miner['auto_battery'] = f\"battery_auto_{ip}\" in request.form"
        )
    insert = (
        needle + "\n"
        "        miner['auto_battery'] = f\"battery_auto_{ip}\" in request.form"
    )
    return text.replace(needle, insert, 1)


def main():
    if not APP.exists():
        raise SystemExit(f"Nicht gefunden: {APP}")

    text = APP.read_text(encoding="utf-8")
    backup = APP.with_name(f"app.py.bak-fix-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(APP, backup)
    print(f"Backup aktueller Datei: {backup.name}")

    if is_broken(text):
        old = newest_backup()
        if old and not is_broken(old.read_text(encoding="utf-8", errors="replace")):
            print(f"Stelle wieder her von: {old.name}")
            text = old.read_text(encoding="utf-8")
        else:
            print("Korrigiere _name_ / _file_ Tippfehler …")
            text = fix_typos(text)

    text = add_auto_battery(text)
    APP.write_text(text, encoding="utf-8")
    print(f"OK – {APP} repariert (auto_battery + Syntax).")


if __name__ == "__main__":
    main()
