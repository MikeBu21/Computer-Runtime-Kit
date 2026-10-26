#!/usr/bin/env python3
"""
1) auto_control.py: Einrückungsfehler beheben, Original-Kelag-Logik + Zusatzwerte
2) Alte enline_reader.py auf dem Pi suchen (falls Backup existiert)
3) Neue enline_reader.py NICHT erzwingen – nur enline_extra_values.py nutzen
"""
from pathlib import Path
from typing import Optional
import re
import shutil
from datetime import datetime

EM = Path.home() / "miner-dashboard/backend/energy_monitor"
AUTO = EM / "auto_control.py"
ER = EM / "enline_reader.py"

LOOP_BLOCK = """                value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein gültiger Enline-Wert")

                print(f"Aktueller Verbrauch: {value} kW")
                with open("energy_value.txt", "w") as f:
                    f.write(str(value))

                try:
                    from enline_extra_values import save_extra_values
                    extra = save_extra_values(enline_page)
                    if extra:
                        print(
                            "Zusatzwerte:",
                            ", ".join(f"{k}={v} kW" for k, v in sorted(extra.items())),
                        )
                except Exception as ex:
                    print(f"Zusatzwerte (optional): {ex}")

                energy_values.append(value)"""


def find_old_enline_reader() -> Optional[Path]:
    if not EM.exists():
        return None
    for path in sorted(EM.rglob("*.py*"), key=lambda p: p.stat().st_mtime):
        if path == ER or "extra_values" in path.name or "fix_" in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "def get_latest_kelag_value" in text and "def start_session" in text:
            if path.resolve() != ER.resolve():
                return path
    for path in Path.home().rglob("enline_reader*.py*"):
        if "energy_monitor" not in str(path):
            continue
        if path.resolve() == ER.resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "def get_latest_kelag_value" in text and "ENERGIE-Plan" not in text:
            # Heuristik: alte Datei enthält oft nicht unseren Standard-LOGIN_URL-Kommentar
            if "read_and_save_all_values" not in text or path.name != "enline_reader.py":
                return path
    return None


def restore_enline_reader_if_possible() -> bool:
    if not ER.exists():
        return False
    current = ER.read_text(encoding="utf-8", errors="ignore")
    if "read_and_save_all_values" not in current and "ENERGIE-Plan Portal" not in current:
        print("enline_reader.py sieht nach Original aus – unverändert.")
        return True

    src = find_old_enline_reader()
    if src:
        broken = ER.with_name(f"enline_reader.py.broken-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(ER, broken)
        shutil.copy2(src, ER)
        print(f"enline_reader wiederhergestellt von: {src}")
        print(f"Kaputte Version: {broken.name}")
        return True

    broken = ER.with_name(f"enline_reader.py.deployed-new-{datetime.now():%Y%m%d-%H%M%S}")
    if not broken.exists():
        shutil.copy2(ER, broken)
    print("WARNUNG: Kein altes enline_reader-Backup gefunden.")
    print(f"Aktuelle Datei gesichert als: {broken.name}")
    print("Bitte alte enline_reader.py manuell zurückkopieren, falls Kelag-Login nicht geht.")
    return False


def fix_auto_control() -> None:
    if not AUTO.exists():
        raise SystemExit(f"Fehlt: {AUTO}")

    text = AUTO.read_text(encoding="utf-8")
    backup = AUTO.with_name(f"auto_control.py.bak-fix-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(AUTO, backup)

    # Import bereinigen
    text = text.replace(
        "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values",
        "from enline_reader import start_session, get_latest_kelag_value",
    )

    # Ganzen kaputten Kelag-Block (inkl. read_and_save / falsche Einrückung) ersetzen
    pattern = re.compile(
        r"(?:[ \t]*values\s*=\s*read_and_save_all_values\(enline_page\)|"
        r"[ \t]*value\s*=\s*get_latest_kelag_value\(enline_page\)).*?"
        r"[ \t]*energy_values\.append\(value\)",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(LOOP_BLOCK, text, count=1)
    elif "save_extra_values" not in text:
        raise SystemExit(
            "Konnte Kelag-Block in auto_control.py nicht finden.\n"
            f"Backup: {backup}\n"
            "Bitte auto_control.py aus 'ELINE AUTOMATIK vor update.txt' neu kopieren."
        )

    AUTO.write_text(text, encoding="utf-8")
    print(f"auto_control.py repariert. Backup: {backup}")


def main():
    print("=== Energy Monitor Fix ===\n")
    restore_enline_reader_if_possible()
    fix_auto_control()
    print("\nStart testen:")
    print("  cd ~/miner-dashboard/backend/energy_monitor")
    print("  source env/bin/activate")
    print("  python auto_control.py")


if __name__ == "__main__":
    main()
