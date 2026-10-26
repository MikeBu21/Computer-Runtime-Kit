#!/usr/bin/env python3
"""auto_control.py robuster machen: Kelag bleibt aktiv, Zusatzwerte optional."""
from pathlib import Path
import re
import shutil
from datetime import datetime


PATH = Path.home() / "miner-dashboard/backend/energy_monitor/auto_control.py"

SAFE_BLOCK = """                values = {}
                try:
                    values = read_and_save_all_values(enline_page) or {}
                except Exception as ex:
                    print(f"Warnung Enline-Zusatzwerte: {ex}")

                value = values.get("kelag")
                if value is None:
                    value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein gültiger Enline-Wert (Kelag)")

                with open("energy_value.txt", "w") as f:
                    f.write(str(value))

                print(f"Aktueller Netz/Kelag: {value} kW")
                if values:
                    print(
                        "Enline:",
                        ", ".join(f"{k}={v} kW" for k, v in sorted(values.items())),
                    )"""


def main():
    if not PATH.exists():
        raise SystemExit(f"Nicht gefunden: {PATH}")

    text = PATH.read_text(encoding="utf-8")
    backup = PATH.with_name(f"auto_control.py.bak-safe-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(PATH, backup)

    if "read_and_save_all_values" not in text:
        text = text.replace(
            "from enline_reader import start_session, get_latest_kelag_value",
            "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values",
        )
        text = text.replace(
            "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values, read_and_save_all_values",
            "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values",
        )

    pattern = re.compile(
        r"values\s*=\s*read_and_save_all_values\(enline_page\).*?"
        r'print\(f"Aktueller Netz/Kelag: \{value\} kW"\).*?'
        r'"\),\s*\)\s*\n',
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(SAFE_BLOCK + "\n", text, count=1)
    else:
        pattern2 = re.compile(
            r"value\s*=\s*get_latest_kelag_value\(enline_page\).*?"
            r'with open\("energy_value\.txt".*?f\.write\(str\(value\)\)\s*\n',
            re.DOTALL,
        )
        text2, n = pattern2.subn(SAFE_BLOCK + "\n", text, count=1)
        if n:
            text = text2
        else:
            raise SystemExit("Konnte Kelag-Block nicht finden – bitte auto_control.py prüfen.")

    PATH.write_text(text, encoding="utf-8")
    print(f"OK: {PATH} (Backup: {backup})")


if __name__ == "__main__":
    main()
