#!/usr/bin/env python3
"""auto_control.py: alle Enline-Werte lesen und Dateien für Dashboard schreiben."""
from pathlib import Path
import re
import shutil
from datetime import datetime


CANDIDATES = [
    Path.home() / "miner-dashboard/backend/energy_monitor/auto_control.py",
    Path(__file__).resolve().parent / "auto_control.py",
]


def find_auto_control() -> Path:
    for path in CANDIDATES:
        if path.exists():
            return path
    raise SystemExit("auto_control.py nicht gefunden.")


def main():
    path = find_auto_control()
    text = path.read_text(encoding="utf-8")
    backup = path.with_name(f"auto_control.py.bak-enline-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)

    if "read_and_save_all_values" not in text:
        text = text.replace(
            "from enline_reader import start_session, get_latest_kelag_value",
            "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values",
        )

    old_block = """                value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein g  ltiger Enline-Wert")

                print(f"Aktueller Verbrauch: {value} kW")
                with open("energy_value.txt", "w") as f:
                    f.write(str(value))"""

    new_block = """                values = read_and_save_all_values(enline_page)
                value = values.get("kelag")
                if value is None:
                    value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein gültiger Enline-Wert (Kelag)")

                print(f"Aktueller Netz/Kelag: {value} kW")
                if values:
                    print(
                        "Enline:",
                        ", ".join(f"{k}={v} kW" for k, v in sorted(values.items())),
                    )"""

    variants = [
        old_block,
        old_block.replace("g  ltiger", "gültiger"),
        """                value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein gültiger Enline-Wert")

                print(f"Aktueller Verbrauch: {value} kW")
                with open("energy_value.txt", "w") as f:
                    f.write(str(value))""",
    ]
    replaced = False
    for block in variants:
        if block in text:
            text = text.replace(block, new_block)
            replaced = True
            break

    if not replaced and "read_and_save_all_values(enline_page)" not in text:
        text, count = re.subn(
            r"value\s*=\s*get_latest_kelag_value\(enline_page\)\s*\n"
            r"\s*if value is None:.*?\n"
            r"\s*with open\(\"energy_value\.txt\".*?f\.write\(str\(value\)\)",
            new_block,
            text,
            count=1,
            flags=re.DOTALL,
        )
        if count == 0:
            raise SystemExit("Konnte Kelag-Block in auto_control.py nicht ersetzen.")

    path.write_text(text, encoding="utf-8")
    print(f"auto_control.py angepasst. Backup: {backup}")


if __name__ == "__main__":
    main()
