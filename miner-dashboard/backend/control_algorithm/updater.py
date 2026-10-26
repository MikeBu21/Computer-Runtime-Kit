# -*- coding: utf-8 -*-
"""
Update des Steuerungsalgorithmus von GitHub.

- Holt nur Algorithmus-Dateien (nicht das ganze Repo blind)
- Bewahrt lokale Einstellungen (control_algorithm_config.json)
- Bewahrt State / History / Energy-Daten
- Passt Energy-Pfade an den lokalen energy_monitor-Ordner an
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.config import BACKEND_DIR, CONFIG_PATH

REPO_ROOT = BACKEND_DIR.parent.parent  # minersteuerung/
GITHUB_REPO = "MikeBu21/piMiner"
DEFAULT_BRANCH = "main"

# Relativ zum Repo-Root: Dateien/Ordner die aktualisiert werden
UPDATE_PATHS = [
    "miner-dashboard/backend/control_algorithm",
    "miner-dashboard/backend/control_algorithm_web.py",
    "miner-dashboard/backend/control_algorithm_daemon.py",
    "miner-dashboard/backend/templates/steuerungsalgorithmus.html",
    "miner-dashboard/backend/requirements-control-algorithm.txt",
]

# Lokal behalten (nie von Git überschreiben)
PRESERVE_FILES = [
    BACKEND_DIR / "control_algorithm_config.json",
    BACKEND_DIR / "control_algorithm_state.json",
    BACKEND_DIR / "control_algorithm_steering_history.json",
]

# Dateien im control_algorithm-Paket, die lokal bleiben sollen
LOCAL_ONLY_RELATIVE = {
    "control_algorithm/paths.py",
    "control_algorithm/updater.py",
}

CRED_FILE = Path.home() / ".config" / "github" / "credentials"


def _read_github_credentials() -> Tuple[Optional[str], Optional[str]]:
    if not CRED_FILE.is_file():
        return None, None
    try:
        lines = [ln.strip() for ln in CRED_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
        if len(lines) == 1 and ":" not in lines[0]:
            return lines[0], None
    except Exception:
        pass
    return None, None


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    user, token = _read_github_credentials()
    if user and token:
        # Credential helper via env – Token nicht in Dateien schreiben
        askpass = tempfile.NamedTemporaryFile("w", delete=False, prefix="git-askpass-", suffix=".sh")
        askpass.write("#!/bin/sh\ncase \"$1\" in\n*Username*) echo \"$GIT_ASKPASS_USER\";;\n*) echo \"$GIT_ASKPASS_TOKEN\";;\nesac\n")
        askpass.close()
        os.chmod(askpass.name, 0o700)
        env["GIT_ASKPASS"] = askpass.name
        env["GIT_ASKPASS_USER"] = user
        env["GIT_ASKPASS_TOKEN"] = token
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["_PIMINER_ASKPASS_CLEANUP"] = askpass.name
    return env


def _cleanup_askpass(env: Dict[str, str]) -> None:
    path = env.pop("_PIMINER_ASKPASS_CLEANUP", None)
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_git(args: List[str], cwd: Path, env: Dict[str, str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _backup_preserved() -> Dict[str, Any]:
    backup: Dict[str, Any] = {}
    for path in PRESERVE_FILES:
        if path.is_file():
            backup[str(path)] = path.read_text(encoding="utf-8")
    return backup


def _restore_preserved(backup: Dict[str, Any]) -> None:
    for path_str, content in backup.items():
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _adapt_energy_paths(root: Path) -> int:
    """
    Schreibt GitHub-Code so um, dass Energy-Daten über den lokalen
    energy_monitor-Ordner gefunden werden.
    """
    changed = 0
    patterns = [
        (
            re.compile(
                r'Path\(([^)]+)\)\s*/\s*["\']energy_stats_24h\.json["\']'
            ),
            r'Path(\1) / "energy_monitor" / "energy_stats_24h.json"',
        ),
        (
            re.compile(
                r'Path\(([^)]+)\)\s*/\s*["\']energy_timeseries_24h\.json["\']'
            ),
            r'Path(\1) / "energy_monitor" / "energy_timeseries_24h.json"',
        ),
        (
            re.compile(
                r'Path\(([^)]+)\)\s*/\s*["\']consumption_history\.json["\']'
            ),
            r'Path(\1) / "energy_monitor" / "consumption_history.json"',
        ),
        (
            re.compile(
                r'os\.path\.join\(([^,]+),\s*["\']energy_stats_24h\.json["\']\)'
            ),
            r'os.path.join(\1, "energy_monitor", "energy_stats_24h.json")',
        ),
        (
            re.compile(r'["\']/energy/["\']'),
            '"energy_monitor"',
        ),
        (
            re.compile(r'/\s*["\']energy["\']\s*/'),
            ' / "energy_monitor" / ',
        ),
    ]

    files: List[Path]
    if root.is_file():
        files = [root]
    else:
        files = [p for p in root.rglob("*.py") if p.name not in ("paths.py", "updater.py")]

    for py in files:
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        original = text
        for regex, repl in patterns:
            text = regex.sub(repl, text)
        text = text.replace(
            ' / "energy_monitor" / "energy_monitor" / ',
            ' / "energy_monitor" / ',
        )
        text = text.replace(
            '"energy_monitor", "energy_monitor"',
            '"energy_monitor"',
        )
        if text != original:
            py.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def _copy_tree(src: Path, dst: Path, skip_names: Optional[set] = None) -> None:
    skip_names = skip_names or set()
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part in skip_names for part in rel.parts):
            continue
        # Lokale Helper nicht überschreiben
        rel_pkg = Path("control_algorithm") / rel
        if str(rel_pkg).replace("\\", "/") in LOCAL_ONLY_RELATIVE:
            continue
        if item.name in ("paths.py", "updater.py") and src.name == "control_algorithm":
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            if item.suffix == ".pyc" or item.name == "__pycache__":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _clear_pycache() -> None:
    pkg = BACKEND_DIR / "control_algorithm"
    cache = pkg / "__pycache__"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
    # Flask-Prozess: Module neu laden
    for name in list(sys.modules):
        if name == "control_algorithm" or name.startswith("control_algorithm."):
            del sys.modules[name]
        if name in ("control_algorithm_web", "control_algorithm_daemon"):
            del sys.modules[name]


def _restart_daemon() -> bool:
    try:
        from control_algorithm.daemon_control import ensure_control_daemon

        return ensure_control_daemon(restart=True)
    except Exception:
        return False


def update_from_github(branch: str = DEFAULT_BRANCH) -> Dict[str, Any]:
    """
    Aktualisiert den Steuerungsalgorithmus von GitHub.

    Returns dict: ok, message|error, details
    """
    user, token = _read_github_credentials()
    if not token:
        return {
            "ok": False,
            "error": (
                "Kein GitHub-Token gefunden. Bitte zuerst "
                "~/setup-github.sh ausführen (Benutzer + Token)."
            ),
        }

    backup = _backup_preserved()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_backup_dir = Path.home() / "backups" / f"steuerungsalgorithmus_pre_update_{stamp}"
    local_backup_dir.mkdir(parents=True, exist_ok=True)

    # Lokalen Stand sichern
    for rel in UPDATE_PATHS:
        src = REPO_ROOT / rel
        if src.exists():
            dst = local_backup_dir / Path(rel).name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst)

    env = _git_env()
    tmp = Path(tempfile.mkdtemp(prefix="ca-update-"))
    try:
        clone_url = f"https://github.com/{GITHUB_REPO}.git"
        # Flacher Sparse-Checkout nur der benötigten Pfade
        r = _run_git(
            ["clone", "--depth", "1", "--filter=blob:none", "--sparse", "-b", branch, clone_url, str(tmp / "repo")],
            cwd=tmp,
            env=env,
            timeout=180,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "git clone fehlgeschlagen").strip()
            # Token nicht in Fehlermeldung lassen
            err = re.sub(r"https://[^@\s]+@", "https://***@", err)
            return {"ok": False, "error": f"GitHub-Clone fehlgeschlagen: {err[:400]}"}

        repo = tmp / "repo"
        r2 = _run_git(
            ["sparse-checkout", "set", *UPDATE_PATHS],
            cwd=repo,
            env=env,
            timeout=60,
        )
        if r2.returncode != 0:
            # Fallback: voller depth-1 ohne sparse (kleine Teilmenge copy)
            pass

        # Commit-Info
        head = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo, env=env, timeout=10)
        commit = (head.stdout or "").strip() or "?"
        log = _run_git(["log", "-1", "--format=%ci %s"], cwd=repo, env=env, timeout=10)
        commit_msg = (log.stdout or "").strip()

        # Energy-Pfade nur in den übernommenen Algorithmus-Dateien anpassen
        adapted = 0
        ca_src = repo / "miner-dashboard" / "backend" / "control_algorithm"
        if ca_src.is_dir():
            adapted += _adapt_energy_paths(ca_src)
        for fname in (
            "control_algorithm_web.py",
            "control_algorithm_daemon.py",
        ):
            fpath = repo / "miner-dashboard" / "backend" / fname
            if fpath.is_file():
                adapted += _adapt_energy_paths(fpath)

        # Dateien übernehmen
        copied: List[str] = []
        for rel in UPDATE_PATHS:
            src = repo / rel
            dst = REPO_ROOT / rel
            if not src.exists():
                continue
            if src.is_dir():
                # Bestehende lokalen only-Dateien merken
                preserve_local = {}
                for only in ("paths.py", "updater.py"):
                    local_f = dst / only
                    if local_f.is_file():
                        preserve_local[only] = local_f.read_text(encoding="utf-8")
                if dst.exists():
                    # Alte .py entfernen die im neuen Stand fehlen? behalten wir mit copytree merge
                    for py in src.rglob("*.py"):
                        if py.name in ("paths.py", "updater.py"):
                            continue
                        rel_py = py.relative_to(src)
                        target = dst / rel_py
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(py, target)
                else:
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "paths.py", "updater.py"))
                for name, content in preserve_local.items():
                    (dst / name).write_text(content, encoding="utf-8")
                # Sicherstellen dass unsere Helper existieren
                copied.append(rel)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(rel)

        # Einstellungen zurückspielen
        _restore_preserved(backup)

        # paths.py / updater.py müssen vorhanden sein (dieses Update)
        # (bereits preserviert bzw. vorher geschrieben)

        _clear_pycache()
        daemon_ok = _restart_daemon()

        details = {
            "commit": commit,
            "commit_info": commit_msg,
            "copied": copied,
            "energy_files_adapted": adapted,
            "backup": str(local_backup_dir),
            "daemon_restarted": daemon_ok,
            "config_preserved": str(CONFIG_PATH) in backup,
        }
        msg = (
            f"Steuerungsalgorithmus aktualisiert ({commit}). "
            f"Einstellungen wurden beibehalten."
        )
        if not daemon_ok:
            msg += " Daemon bitte manuell neu starten."
        return {"ok": True, "message": msg, "details": details}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "GitHub-Timeout – bitte später erneut versuchen."}
    except Exception as e:
        # Einstellungen wiederherstellen falls schon überschrieben
        try:
            _restore_preserved(backup)
        except Exception:
            pass
        return {"ok": False, "error": f"Update-Fehler: {e}"}
    finally:
        _cleanup_askpass(env)
        shutil.rmtree(tmp, ignore_errors=True)
