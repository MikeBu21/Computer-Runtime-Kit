# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import public_update as pu


def test_upload_button_hidden_while_newer_version_or_upload():
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=False, local_version=2, remote_version=3
    ) is False
    assert pu.upload_allowed(
        is_publisher=True, uploading=True, downloading=False, local_version=3, remote_version=3
    ) is False
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=False, local_version=3, remote_version=None
    ) is False
    assert pu.upload_allowed(
        is_publisher=False, uploading=False, downloading=False, local_version=3, remote_version=3
    ) is False
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=False, local_version=3, remote_version=3
    ) is True


def test_standard_overlay_has_no_personal_material():
    files = pu.standard_overlay()
    miners = json.loads(files["miner-dashboard/backend/miners.json"])
    settings = json.loads(files["miner-dashboard/backend/dashboard_settings.json"])
    telegram = json.loads(files["miner-dashboard/backend/telegram_config.json"])
    algo = json.loads(files["miner-dashboard/backend/control_algorithm_config.json"])
    chats = json.loads(files["miner-dashboard/backend/telegram_chats.json"])
    assert miners[0]["ip"] == "192.0.2.10"
    assert miners[0]["password"] == ""
    assert miners[0]["note"] == "Beispielgerät"
    assert settings["login_password"] == ""
    assert settings["smartmeter_url"] == ""
    assert telegram["telegram_token"] == ""
    assert chats == []
    assert algo["storage_kwh"] == 0
    assert algo["city"] == ""
    assert algo["lat"] is None
    blob = "\n".join(files.values())
    assert "0" not in blob
    assert "Beispiel" not in blob


def test_scan_rejects_personal_token(tmp_path: Path):
    secret = "123456789:AA" + "b" * 30
    (tmp_path / "note.txt").write_text(f"token {secret}\n", encoding="utf-8")
    problems = pu.scan_tree(tmp_path, [])
    assert problems == ["note.txt"]


def test_scan_rejects_copied_secret_string(tmp_path: Path):
    (tmp_path / "miner.json").write_text('{"ip": "192.0.2.10"}\n', encoding="utf-8")
    problems = pu.scan_tree(tmp_path, ["192.0.2.10"])
    assert problems == ["miner.json"]


def test_download_installs_only_program_code(tmp_path: Path):
    snapshot = tmp_path / "snapshot"
    dest = tmp_path / "pi"
    personal = {
        "miner-dashboard/backend/miners.json": '[{"password": "geheim", "ip": "192.0.2.10"}]\n',
        "miner-dashboard/backend/control_algorithm_config.json": '{"storage_kwh": 41, "city": "Zuhause"}\n',
        "miner-dashboard/backend/control_algorithm_learning_store.json": '{"neurons": [1, 2, 3]}\n',
        "miner-dashboard/backend/telegram_chats.json": "[0]\n",
        "miner-dashboard/backend/dashboard_settings.json": '{"login_password": "geheim"}\n',
        "logs/history.json": '{"days": [1]}\n',
        "miner-dashboard/backend/app.py": "print('alt')\n",
    }
    incoming = {
        "miner-dashboard/backend/miners.json": "[]\n",
        "miner-dashboard/backend/control_algorithm_config.json": '{"storage_kwh": 0}\n',
        "miner-dashboard/backend/control_algorithm_learning_store.json": "{}\n",
        "miner-dashboard/backend/telegram_chats.json": "[]\n",
        "miner-dashboard/backend/dashboard_settings.json": '{"login_password": ""}\n',
        "logs/history.json": "{}\n",
        "README.md": "standard\n",
        "homeassistant/config/automations.yaml": "[]\n",
        "miner-dashboard/docs/systemd-user/miner-dashboard.service": "[Service]\n",
        "miner-dashboard/backend/app.py": "print('neu')\n",
        "miner-dashboard/backend/templates/dashboard.html": "<p>neu</p>\n",
        "scripts/example.sh": "echo neu\n",
    }
    for rel, content in personal.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for rel, content in incoming.items():
        path = snapshot / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    copied = pu._copy_download(snapshot, dest)

    # Code + Setup-Assets (z. B. systemd-User-Unit), keine persönlichen JSON/Logs.
    assert copied == 4
    assert (dest / "miner-dashboard/backend/app.py").read_text(encoding="utf-8") == "print('neu')\n"
    assert "neu" in (dest / "miner-dashboard/backend/templates/dashboard.html").read_text(encoding="utf-8")
    assert (dest / "scripts/example.sh").read_text(encoding="utf-8") == "echo neu\n"
    assert (dest / "miner-dashboard/docs/systemd-user/miner-dashboard.service").is_file()
    for rel, content in personal.items():
        if rel.endswith(".py"):
            continue
        assert (dest / rel).read_text(encoding="utf-8") == content
    assert not (dest / "README.md").exists()
    assert pu.is_installable_code("miner-dashboard/backend/app.py")
    assert pu.is_installable_code("miner-dashboard/backend/control_algorithm/config.py")
    assert pu.is_installable_code("miner-dashboard/backend/miners.json") is False
    assert pu.is_installable_code("miner-dashboard/backend/control_algorithm_learning_store.json") is False
    assert pu.is_installable_code("miner-dashboard/backend/shared_settings/helper.py") is False
    assert pu.is_installable_code("backup/tool.sh") is False
    assert pu.is_installable_code("logs/error_reports/outbox/pack.py") is False
    assert pu.is_installable_code("homeassistant/config/custom_components/local_plugin.py") is False
    assert pu.is_installable_code("config/profile.py") is False
    assert pu.is_installable_code("scripts/tailscale_notify/install.local.sh") is False
    assert pu.is_installable_code("scripts/export/dump.sh") is False


def test_personal_code_file_is_not_replaced(tmp_path: Path):
    snapshot = tmp_path / "snapshot"
    dest = tmp_path / "pi"
    secret = "nur-dieses-geraet-passwort"
    local_script = "print('lokal " + secret + "')\n"
    (dest / "miner-dashboard/backend").mkdir(parents=True)
    (dest / "miner-dashboard/backend/miners.json").write_text(
        json.dumps([{"password": secret, "ip": "192.0.2.10"}]),
        encoding="utf-8",
    )
    (dest / "pause_miner.py").write_text(local_script, encoding="utf-8")
    (snapshot / "pause_miner.py").parent.mkdir(parents=True)
    (snapshot / "pause_miner.py").write_text("print('entwickler')\n", encoding="utf-8")
    (snapshot / "miner-dashboard/backend").mkdir(parents=True)
    (snapshot / "miner-dashboard/backend/app.py").write_text("print('neu')\n", encoding="utf-8")

    copied = pu._copy_download(snapshot, dest)

    assert copied == 1
    assert (dest / "pause_miner.py").read_text(encoding="utf-8") == local_script
    assert (dest / "miner-dashboard/backend/app.py").read_text(encoding="utf-8") == "print('neu')\n"
    assert secret in (dest / "miner-dashboard/backend/miners.json").read_text(encoding="utf-8")


def test_example_tree_passes_scan(tmp_path: Path):
    for rel, content in pu.standard_overlay().items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    assert pu.scan_tree(tmp_path, ["192.0.2.10", "geheimes-passwort"]) == []


def test_ha_entity_id_is_not_a_scan_needle():
    assert pu._scan_needle_usable("sensor.solarnet_pv_leistung") is False
    assert pu._scan_needle_usable("switch.beispiel_geraet") is False
    assert pu._scan_needle_usable("Beispiel") is False
    assert pu._scan_needle_usable("192.0.2.10") is True
    assert pu._scan_needle_usable("0") is True
    secret = "123456789:AA" + "b" * 30
    assert pu._scan_needle_usable(secret) is True


def test_scan_ignores_matching_ha_entity_default(tmp_path: Path):
    """Lokale Config-Entity-ID darf Kit-Code mit leerem Default nicht blockieren."""
    entity = "sensor.solarnet_pv_leistung"
    kit = tmp_path / "kit"
    (kit / "miner-dashboard/backend").mkdir(parents=True)
    (kit / "miner-dashboard/backend/ha_entity_mapping.py").write_text(
        'def _default_fields():\n'
        '    return {"pv": {"enabled": True, "entity_id": "", "sensors": []}}\n',
        encoding="utf-8",
    )
    secrets = {entity, "192.0.2.10"}
    assert pu.scan_tree(kit, secrets) == []


def test_scan_still_rejects_real_token_in_kit(tmp_path: Path):
    secret = "123456789:AA" + "b" * 30
    (tmp_path / "leak.py").write_text(f'TOKEN = "{secret}"\n', encoding="utf-8")
    assert pu.scan_tree(tmp_path, []) == ["leak.py"]


def test_soft_scan_survives_upstream_hard_scan(tmp_path: Path):
    upstream = tmp_path / "public_update.py"
    upstream.write_text(
        "def _copy_download(snapshot, dest_root=None):\n"
        "    return 0\n"
        "\n"
        "def _download_worker():\n"
        "    version = 1\n"
        "    problems = scan_tree(DOWNLOAD_DIR, collect_sensitive_values(REPO_ROOT))\n"
        "    if problems:\n"
        "        raise RuntimeError(\n"
        '            "Download abgebrochen: die öffentliche Version enthält persönliche Daten "\n'
        '            f"({len(problems)} Fundstelle(n), z. B. {problems[0]})."\n'
        "        )\n"
        "    _copy_download(DOWNLOAD_DIR)\n",
        encoding="utf-8",
    )
    pu._ensure_soft_download_scan_file(upstream)
    text = upstream.read_text(encoding="utf-8")
    assert "DOWNLOAD_SCAN_SOFT_V1" in text
    assert "scan_tree(DOWNLOAD_DIR, [])" in text
    assert "collect_sensitive_values(REPO_ROOT)" not in text
    assert "_copy_download(DOWNLOAD_DIR)\n        _ensure_soft_download_scan_file()" in text
    defs = text.count("def _ensure_soft_download_scan_file(")
    assert defs >= 1
    pu._ensure_soft_download_scan_file(upstream)
    again = upstream.read_text(encoding="utf-8")
    assert again.count("def _ensure_soft_download_scan_file(") == defs
    assert "collect_sensitive_values(REPO_ROOT)" not in again


def test_auto_update_waits_about_a_minute_then_downloads():
    action, version, seen = pu.auto_update_step(16, 15, False, None, None, 1000.0)
    assert action == "wait"
    assert version == 16
    action, version, seen = pu.auto_update_step(16, 15, False, version, seen, 1030.0)
    assert action == "wait"
    action, version, seen = pu.auto_update_step(16, 15, False, version, seen, 1060.0)
    assert action == "download"
    assert version == 16


def test_auto_update_ignores_same_version_and_clears_when_busy():
    assert pu.auto_update_step(15, 15, False, None, None, 1000.0)[0] == "idle"
    assert pu.auto_update_step(14, 15, False, 16, 1000.0, 1100.0)[0] == "idle"
    assert pu.auto_update_step(None, 15, False, 16, 1000.0, 1100.0)[0] == "idle"
    assert pu.auto_update_step(16, 15, True, 16, 1000.0, 1100.0)[0] == "idle"


def test_pull_if_newer_leaves_the_current_version_alone(monkeypatch):
    monkeypatch.setattr(pu, "fetch_remote_version", lambda: 15)
    monkeypatch.setattr(pu, "load_state", lambda: {"local_version": 15})
    monkeypatch.setattr(pu, "_job", lambda: {"uploading": False, "downloading": False})
    monkeypatch.setattr(pu, "start_download", lambda: (_ for _ in ()).throw(AssertionError("kein Download")))
    assert pu.pull_if_newer() is False


def test_pull_if_newer_starts_when_the_web_version_is_higher(monkeypatch):
    monkeypatch.setattr(pu, "fetch_remote_version", lambda: 16)
    monkeypatch.setattr(pu, "load_state", lambda: {"local_version": 15})
    monkeypatch.setattr(pu, "_job", lambda: {"uploading": False, "downloading": False})
    monkeypatch.setattr(pu, "start_download", lambda: (True, "ok"))
    assert pu.pull_if_newer() is True


def test_default_fields_have_empty_entity_ids():
    from ha_entity_mapping import _default_fields

    for name, field in _default_fields().items():
        assert field.get("entity_id") == "", name
