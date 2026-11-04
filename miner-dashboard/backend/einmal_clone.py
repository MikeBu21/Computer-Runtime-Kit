# -*- coding: utf-8 -*-
"""Einmal-Klon für genau diesen Pi.

Ausgeliefert wird nur, was schon unter ~/.local/share/piminer-einmal liegt.
Fehlen Skript oder Archiv, antwortet die Route mit 404. Dadurch bekommen
andere Pis beim späteren Veröffentlichen von app.py dieses Paket nicht.
Es wird nichts hochgeladen und kein Tailscale-Schlüssel geschrieben.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from pathlib import Path

from flask import Flask, Response, abort, request, send_file

EINMAL_DIR = Path.home() / ".local" / "share" / "piminer-einmal"
SCRIPT_NAME = "install.sh"
ARCHIVE_NAME = "paket.tar.gz"
ORIGIN_MARK = "__EINMAL_ORIGIN__"

# Zusammengesetzt, damit das Muster selbst kein Schlüssel-Präfix im Paket ist.
_TSKEY_RE = re.compile("ts" + "key-" + r"[A-Za-z0-9_-]+")

# Lernstände, die der neue Pi leer braucht. Persönliche Statistik bleibt draußen.
_EMPTY_LEARNING = {
    "control_algorithm_learning_store.json": "{}\n",
    "control_algorithm_state.json": "{}\n",
    "control_algorithm_steering_history.json": (
        '{"sessions": [], "open_by_ip": {}, "events": []}\n'
    ),
}


def register(app: Flask) -> None:
    @app.get("/einmal/install.sh")
    def einmal_install_sh():
        path = EINMAL_DIR / SCRIPT_NAME
        if not path.is_file():
            abort(404)
        text = path.read_text(encoding="utf-8")
        if ORIGIN_MARK not in text:
            abort(404)
        origin = request.host_url.rstrip("/")
        body = text.replace(ORIGIN_MARK, origin)
        response = Response(body, mimetype="text/x-shellscript; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/einmal/paket.tar.gz")
    def einmal_paket():
        path = EINMAL_DIR / ARCHIVE_NAME
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype="application/gzip", conditional=True, max_age=0)
        response.headers["Cache-Control"] = "no-store"
        return response


def build_package(repo: Path | None = None) -> Path:
    """Baut das gestrippte Archiv lokal. Ruft den Publisher nicht auf."""
    import public_update as pu

    repo = Path(repo or pu.REPO_ROOT)
    EINMAL_DIR.mkdir(parents=True, exist_ok=True)
    staging = EINMAL_DIR / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    pu.export_snapshot(staging, 0, repo)
    _drop_personal_runtime(staging, pu)
    _write_fresh_files(staging)
    secrets = set(pu.collect_sensitive_values(repo))
    secrets.update(_extra_secrets(repo, pu))
    pu.redact_tree(staging, secrets)
    _scrub_token_patterns(staging, pu)
    problems = pu.scan_tree(staging, secrets)
    problems.extend(_forbidden_paths(staging, pu))
    if problems:
        raise RuntimeError(
            "Paket enthält noch persönliche Pfade: " + ", ".join(problems[:40])
        )
    _assert_fresh_learning(staging)
    archive = EINMAL_DIR / ARCHIVE_NAME
    partial = archive.with_name(archive.name + ".partial")
    if partial.exists():
        partial.unlink()
    with tarfile.open(partial, "w:gz") as tar:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(staging).as_posix()
            if arcname.startswith("/") or ".." in Path(arcname).parts:
                raise RuntimeError(f"Unsicherer Archivpfad: {arcname}")
            tar.add(path, arcname=arcname)
    os.replace(partial, archive)
    shutil.rmtree(staging)
    return archive


def _drop_personal_runtime(root: Path, pu) -> None:
    for path in list(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        name = path.name
        drop = False
        if name in pu.SKIP_FILE_NAMES or name == "tailscaled.state":
            drop = True
        elif name.endswith(("_value.txt", "_avg.txt")) or name.startswith("energy_value.txt"):
            drop = True
        elif rel.startswith(pu.SKIP_PREFIXES):
            drop = True
        elif "/tailscale_notify/" in f"/{rel}" and name in {"state.json", "tailscaled.state"}:
            drop = True
        if drop:
            path.unlink()


def _write_fresh_files(root: Path) -> None:
    backend = root / "miner-dashboard" / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    for name, content in _EMPTY_LEARNING.items():
        (backend / name).write_text(content, encoding="utf-8")
    from energie_steuerung import default_settings

    (backend / "energie_steuerung.json").write_text(
        json.dumps(default_settings(), indent=2) + "\n",
        encoding="utf-8",
    )
    (backend / "commands.json").write_text("{}\n", encoding="utf-8")
    (backend / "gpio_pin_status.json").write_text("{}\n", encoding="utf-8")
    (backend / "gpio_pins.json").write_text("[]\n", encoding="utf-8")


def _extra_secrets(repo: Path, pu) -> set[str]:
    """Tailscale-Laufzeit und Schlüssel, die die Skip-Liste nicht als Text erfasst."""
    found: set[str] = set()
    state = repo / "scripts" / "tailscale_notify" / "linux" / "state.json"
    if state.is_file():
        text = state.read_text(encoding="utf-8", errors="replace")
        for match in pu._IPV4_RE.findall(text):
            if not match.startswith("192.0.2.") and match not in {"127.0.0.1", "0.0.0.0"}:
                found.add(match)
        for match in pu._TOKEN_RE.findall(text):
            found.add(match)
        for match in _TSKEY_RE.findall(text):
            found.add(match)
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        if isinstance(data, dict):
            dns = str(data.get("dns_name") or "").strip()
            if dns.endswith(".ts.net") and len(dns) >= 6:
                found.add(dns)
            host = str(data.get("hostname") or "").strip()
            if host and host not in {"piminer", "localhost"} and len(host) >= 6:
                found.add(host)
            chats = data.get("known_chat_ids") or []
            if isinstance(chats, list):
                for item in chats:
                    pu._add_secret(found, item)
    for rel in (
        "miner-dashboard/backend/energiegemeinschaft_settings.json",
        "miner-dashboard/backend/boersenhandel_settings.json",
        "miner-dashboard/backend/bms_test_settings.json",
        "miner-dashboard/backend/bms_steuerung_settings.json",
        "miner-dashboard/backend/grundverbrauch_rechner.json",
        "miner-dashboard/backend/energy_monitor/grundverbrauch_rechner.json",
    ):
        path = repo / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pu._TOKEN_RE.findall(text):
            found.add(match)
        for match in _TSKEY_RE.findall(text):
            found.add(match)
        for match in pu._GITHUB_TOKEN_RE.findall(text):
            found.add(match)
        try:
            parsed = json.loads(text) if text.strip().startswith(("{", "[")) else None
        except json.JSONDecodeError:
            parsed = None
        _collect_named_secrets(parsed, found, pu)
    found.discard(pu.EXAMPLE_IP)
    found.discard("piminer")
    return {item for item in found if item and len(str(item)) >= 6 and str(item) != "piminer"}


def _collect_named_secrets(node, found: set[str], pu) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            if name in pu.SECRET_KEYS or name in {
                "tailscale_auth_key",
                "auth_key",
                "invite_code",
                "modbus_host",
            }:
                if isinstance(value, list):
                    for item in value:
                        pu._add_secret(found, item)
                else:
                    pu._add_secret(found, value)
            _collect_named_secrets(value, found, pu)
        return
    if isinstance(node, list):
        for item in node:
            _collect_named_secrets(item, found, pu)


def _scrub_token_patterns(root: Path, pu) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".gz", ".zip", ".db"}:
            continue
        if path.stat().st_size > 8_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        updated = pu._TOKEN_RE.sub("", text)
        updated = pu._GITHUB_TOKEN_RE.sub("", updated)
        updated = _TSKEY_RE.sub("", updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _forbidden_paths(root: Path, pu) -> list[str]:
    allowed_empty = set(_EMPTY_LEARNING)
    problems: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        name = path.name
        if name in pu.SKIP_FILE_NAMES and name not in allowed_empty:
            problems.append(rel)
        elif name == "tailscaled.state" or name.endswith(("_value.txt", "_avg.txt")):
            problems.append(rel)
        elif "/tailscale_notify/" in f"/{rel}" and name == "state.json":
            problems.append(rel)
    return problems


def _assert_fresh_learning(root: Path) -> None:
    backend = root / "miner-dashboard" / "backend"
    for name, content in _EMPTY_LEARNING.items():
        path = backend / name
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"Lernstand ist nicht leer: {name}")
    from energie_steuerung import default_settings

    stored = json.loads((backend / "energie_steuerung.json").read_text(encoding="utf-8"))
    if stored != default_settings():
        raise RuntimeError("Energie-Einstellungen sind nicht die Beispielwerte.")
