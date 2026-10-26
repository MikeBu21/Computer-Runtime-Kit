#!/usr/bin/env python3
"""
Stellt enline_reader.py + auto_control.py auf dem Pi wieder her.
Auf dem Pi ausführen: python3 restore_original_enline_on_pi.py
"""
from pathlib import Path
import shutil
from datetime import datetime

EM = Path.home() / "miner-dashboard/backend/energy_monitor"
ER = EM / "enline_reader.py"
AC = EM / "auto_control.py"
ORIG = EM / "enline_reader_original.py"


def backup(path: Path) -> None:
    if path.exists():
        dest = path.with_name(f"{path.name}.vor-restore-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, dest)
        print(f"Backup: {dest.name}")


def restore_enline_reader() -> None:
    backup(ER)

    # Altes Backup auf dem Pi (falls vor unserem Deploy)
    best = None
    for p in sorted(EM.glob("enline_reader.py*"), key=lambda x: x.stat().st_mtime):
        name = p.name
        if p == ER:
            continue
        if any(x in name for x in ("deployed-new", "broken", "original", "vor-restore")):
            continue
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "def get_latest_kelag_value" in text and "def start_session" in text:
            if "read_and_save_all_values" not in text and "ENERGIE-Plan Portal" not in text:
                best = p
                break
            if len(text) < 8000 and best is None:
                best = p

    if best:
        shutil.copy2(best, ER)
        print(f"enline_reader wiederhergestellt von: {best.name}")
        return

    if ORIG.exists():
        shutil.copy2(ORIG, ER)
        print("enline_reader wiederhergestellt von: enline_reader_original.py")
        return

    raise SystemExit(
        "Keine Original-datei gefunden. Bitte enline_reader_original.py per scp kopieren."
    )


def restore_auto_control() -> None:
    backup(AC)

    for p in sorted(EM.glob("auto_control.py.bak*"), key=lambda x: x.stat().st_mtime):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "get_latest_kelag_value(enline_page)" in text and "read_and_save_all_values" not in text:
            shutil.copy2(p, AC)
            print(f"auto_control wiederhergestellt von: {p.name}")
            return

    # Minimaler Kelag-Block falls kein Backup
    text = AC.read_text(encoding="utf-8")
    text = text.replace(
        "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values",
        "from enline_reader import start_session, get_latest_kelag_value",
    )
    text = text.replace(
        "from enline_reader import start_session, get_latest_kelag_value, read_and_save_all_values, read_and_save_all_values",
        "from enline_reader import start_session, get_latest_kelag_value",
    )

    import re

    block = """                value = get_latest_kelag_value(enline_page)
                if value is None:
                    raise ValueError("Kein gültiger Enline-Wert")

                print(f"Aktueller Verbrauch: {value} kW")
                with open("energy_value.txt", "w") as f:
                    f.write(str(value))

                try:
                    from enline_extra_values import save_extra_values
                    extra = save_extra_values(enline_page)
                    if extra:
                        print("Zusatzwerte:", ", ".join(f"{k}={v}" for k, v in sorted(extra.items())))
                except Exception:
                    pass

                energy_values.append(value)"""

    pattern = re.compile(
        r"(?:[ \t]*values\s*=\s*read_and_save_all_values.*?|"
        r"[ \t]*value\s*=\s*get_latest_kelag_value.*?)"
        r"[ \t]*energy_values\.append\(value\)",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    AC.write_text(text, encoding="utf-8")
    print("auto_control: Kelag-Block vereinfacht (Backup fehlte).")


def main():
    print("=== Wiederherstellung Original Enline ===\n")
    restore_enline_reader()
    restore_auto_control()
    print("\nFertig. Test:")
    print("  cd", EM)
    print("  source env/bin/activate")
    print("  python auto_control.py")


if __name__ == "__main__":
    main()
