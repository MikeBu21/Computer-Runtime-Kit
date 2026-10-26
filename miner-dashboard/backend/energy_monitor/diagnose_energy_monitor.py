#!/usr/bin/env python3
"""Energy-Monitor auf dem Pi testen und typische Fehler anzeigen."""
import os
import subprocess
import sys
from pathlib import Path

EM = Path.home() / "miner-dashboard/backend/energy_monitor"
AC = EM / "auto_control.py"
ER = EM / "enline_reader.py"


def ok(msg):
    print(f"OK  {msg}")


def fail(msg):
    print(f"FEHLER  {msg}")


def main():
    print("=== Energy Monitor Diagnose ===\n")
    print(f"Ordner: {EM}")

    if not EM.is_dir():
        fail(f"Ordner fehlt: {EM}")
        return 1

    for name in ("auto_control.py", "enline_reader.py"):
        p = EM / name
        if p.exists():
            ok(f"{name} vorhanden")
        else:
            fail(f"{name} fehlt")

    venvs = []
    for vname in ("venv", "env", ".venv"):
        act = EM / vname / "bin" / "activate"
        if act.exists():
            venvs.append(vname)
    if venvs:
        ok(f"Virtuelle Umgebung: {', '.join(venvs)}")
    else:
        fail("Kein venv/env gefunden – Start-Skript bricht ab")

    backups = sorted(EM.glob("enline_reader.py.bak*")) + sorted(EM.glob("enline_reader.py.bak-*"))
    if backups:
        ok(f"Enline-Backup(s): {len(backups)} – z.B. {backups[-1].name}")
    else:
        print("HINWEIS  Kein enline_reader.py-Backup im Ordner")

    cfg = EM / "enline_config.json"
    if cfg.exists():
        ok("enline_config.json vorhanden")
    else:
        print("HINWEIS  enline_config.json fehlt (Login nur über auto_control möglich)")

    print("\n--- Python-Import testen ---")
    py = sys.executable
    for vname in venvs or [""]:
        if vname:
            py = str(EM / vname / "bin" / "python")
        cmd = [py, "-c", "import enline_reader; print('import enline_reader OK')"]
        try:
            r = subprocess.run(cmd, cwd=EM, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                ok(f"Import mit {py}")
                print(r.stdout.strip())
            else:
                fail(f"Import mit {py}")
                print(r.stderr or r.stdout)
        except Exception as e:
            fail(str(e))

    print("\n--- Syntax auto_control ---")
    if AC.exists():
        r = subprocess.run([sys.executable, "-m", "py_compile", str(AC)], capture_output=True, text=True)
        if r.returncode == 0:
            ok("auto_control.py Syntax")
        else:
            fail("auto_control.py Syntax")
            print(r.stderr)

    print("\n--- Letzte Zeilen (falls Log existiert) ---")
    for log in (EM / "energy_monitor.log", EM / "auto_control.log"):
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-15:]
            print(f"\n{log}:")
            print("\n".join(lines))

    print("\n=== Ende ===")
    print("Manuell starten (Fehlermeldung lesen):")
    if venvs:
        print(f"  cd {EM} && source {venvs[0]}/bin/activate && python auto_control.py")
    else:
        print(f"  cd {EM} && python3 auto_control.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
