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

    assert copied == 3
    assert (dest / "miner-dashboard/backend/app.py").read_text(encoding="utf-8") == "print('neu')\n"
    assert "neu" in (dest / "miner-dashboard/backend/templates/dashboard.html").read_text(encoding="utf-8")
    assert (dest / "scripts/example.sh").read_text(encoding="utf-8") == "echo neu\n"
    for rel, content in personal.items():
        if rel.endswith(".py"):
            continue
        assert (dest / rel).read_text(encoding="utf-8") == content
    assert not (dest / "README.md").exists()
    assert not (dest / "miner-dashboard/docs/systemd-user/miner-dashboard.service").exists()
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
