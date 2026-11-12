# -*- coding: utf-8 -*-
import contextlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import public_update as pu


def test_upload_button_only_on_publisher_and_not_while_busy():
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=False, local_version=2, remote_version=3
    ) is True
    assert pu.upload_allowed(
        is_publisher=True, uploading=True, downloading=False, local_version=3, remote_version=3
    ) is False
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=True, local_version=3, remote_version=3
    ) is False
    assert pu.upload_allowed(
        is_publisher=True, uploading=False, downloading=False, local_version=3, remote_version=None
    ) is True
    assert pu.upload_allowed(
        is_publisher=False, uploading=False, downloading=False, local_version=3, remote_version=3
    ) is False


def test_next_version_counts_past_web_and_local():
    assert pu.next_version(31, 33) == 34
    assert pu.next_version(35, 33) == 36
    assert pu.next_version(33, None) == 34


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
    assert "myminingfarm1986" not in blob


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

    result = pu._copy_download(snapshot, dest)

    # Code + Setup-Assets (z. B. systemd-User-Unit), keine persönlichen JSON/Logs.
    assert result["copied"] == 4
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


def test_personal_code_file_is_updated_with_backup(tmp_path: Path):
    snapshot = tmp_path / "snapshot"
    dest = tmp_path / "pi"
    backup = tmp_path / "rollback"
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

    result = pu._copy_download(snapshot, dest, backup_dir=backup)

    assert result["copied"] == 2
    assert result["personal_overwritten"] == ["pause_miner.py"]
    assert (dest / "pause_miner.py").read_text(encoding="utf-8") == "print('entwickler')\n"
    assert (backup / "files/pause_miner.py").read_text(encoding="utf-8") == local_script
    assert json.loads((backup / "rollback.json").read_text(encoding="utf-8"))["replaced"] == ["pause_miner.py"]
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


def test_auto_update_is_on_after_boot_and_off_only_in_ram(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(pu, "PUBLISHER_FILE", tmp_path / "publisher")
    monkeypatch.setattr(pu, "RAM_DIR", tmp_path / "ram")
    monkeypatch.setattr(pu, "AUTO_UPDATE_OFF_FLAG", tmp_path / "ram" / "auto_update_off")
    assert pu.auto_update_active() is True
    assert pu.set_auto_update(False)[0] is True
    assert pu.auto_update_active() is False
    assert pu.set_auto_update(True)[0] is True
    assert pu.auto_update_active() is True
    (tmp_path / "publisher").write_text("1\n", encoding="utf-8")
    assert pu.auto_update_active() is False
    assert pu.set_auto_update(True)[0] is False


def test_pull_if_newer_skips_when_auto_update_off(monkeypatch):
    monkeypatch.setattr(pu, "auto_update_active", lambda: False)
    monkeypatch.setattr(pu, "fetch_remote_version", lambda use_cache=False: 99)
    monkeypatch.setattr(pu, "start_download", lambda: (_ for _ in ()).throw(AssertionError("kein Download")))
    assert pu.pull_if_newer() is False


def test_pull_if_newer_leaves_the_current_version_alone(monkeypatch):
    monkeypatch.setattr(pu, "auto_update_active", lambda: True)
    monkeypatch.setattr(pu, "fetch_remote_version", lambda use_cache=False: 15)
    monkeypatch.setattr(pu, "load_state", lambda: {"local_version": 15})
    monkeypatch.setattr(pu, "_job", lambda: {"uploading": False, "downloading": False})
    monkeypatch.setattr(pu, "start_download", lambda: (_ for _ in ()).throw(AssertionError("kein Download")))
    assert pu.pull_if_newer() is False


def test_pull_if_newer_starts_when_the_web_version_is_higher(monkeypatch):
    monkeypatch.setattr(pu, "auto_update_active", lambda: True)
    monkeypatch.setattr(pu, "fetch_remote_version", lambda use_cache=False: 16)
    monkeypatch.setattr(pu, "load_state", lambda: {"local_version": 15})
    monkeypatch.setattr(pu, "_job", lambda: {"uploading": False, "downloading": False})
    monkeypatch.setattr(pu, "start_download", lambda: (True, "ok"))
    assert pu.pull_if_newer() is True


def test_default_fields_have_empty_entity_ids():
    from ha_entity_mapping import _default_fields

    for name, field in _default_fields().items():
        assert field.get("entity_id") == "", name


class HaRepairTests(unittest.TestCase):
    """Leere Home-Assistant-venv nach dem Code-Apply reparieren, Update nicht abbrechen."""

    def _layout(self, root: Path, *, hass: bool) -> pu._HaLayout:
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python3"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        config = root / "config"
        config.mkdir()
        (config / ".HA_VERSION").write_text("2026.2.3\n", encoding="utf-8")
        hass_bin = venv_bin / "hass"
        if hass:
            hass_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hass_bin.chmod(0o755)
        return pu._HaLayout(
            root=root,
            venv_dir=root / "venv",
            hass_bin=hass_bin,
            config_dir=config,
            tz="Europe/Vienna",
        )

    def test_executable_hass_skips_repair(self):
        self.assertFalse(pu._python_satisfies("3.11.2", ">=3.13.2"))
        self.assertTrue(pu._python_satisfies("3.13.2", ">=3.13.2"))
        self.assertTrue(pu._python_satisfies("3.13.5", ">=3.13.2"))
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), hass=True)
            original = layout.hass_bin.read_text(encoding="utf-8")
            config_before = sorted(path.name for path in layout.config_dir.iterdir())

            def forbid_commands(args, timeout=120):
                raise AssertionError("Reparatur darf bei ausführbarem hass nicht laufen")

            with patch.object(pu, "_resolve_ha_layout", lambda: layout), patch.object(
                pu, "_run_cmd", forbid_commands
            ), patch.object(
                pu, "_poll_home_assistant", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein Poll"))
            ):
                message = pu.ensure_home_assistant()

            self.assertIn("ausführbar", message)
            self.assertNotIn("Warnung:", message)
            self.assertEqual(layout.hass_bin.read_text(encoding="utf-8"), original)
            self.assertEqual(sorted(path.name for path in layout.config_dir.iterdir()), config_before)
            self.assertEqual((layout.config_dir / ".HA_VERSION").read_text(encoding="utf-8"), "2026.2.3\n")

    def test_old_python_missing_hass_uses_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = self._layout(root, hass=False)
            calls = []

            def fake_run(args, timeout=120):
                args = [str(item) for item in args]
                calls.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(args, 1, "", "missing")
                if args[:2] == ["docker", "pull"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["docker", "ps"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["docker", "run"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:1] == ["systemctl"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if "pip" in args:
                    raise AssertionError("pip darf bei zu altem Python nicht laufen")
                raise AssertionError("unerwartet: " + " ".join(args))

            with patch.object(pu, "_resolve_ha_layout", lambda: layout), patch.object(
                pu, "_venv_python_version", lambda python: "3.11.2"
            ), patch.object(pu, "_requires_python", lambda version: ">=3.13.2"), patch.object(
                pu, "_run_cmd", fake_run
            ), patch.object(pu, "_poll_home_assistant", lambda *a, **k: True):
                message = pu.ensure_home_assistant()

            self.assertNotIn("pip", " ".join(" ".join(cmd) for cmd in calls))
            self.assertIn(
                ["docker", "pull", "ghcr.io/home-assistant/home-assistant:2026.2.3"],
                calls,
            )
            run_cmds = [cmd for cmd in calls if cmd[:2] == ["docker", "run"]]
            self.assertEqual(len(run_cmds), 1)
            self.assertIn("--network=host", run_cmds[0])
            self.assertIn(str(layout.config_dir) + ":/config", run_cmds[0])
            self.assertIn("ghcr.io/home-assistant/home-assistant:2026.2.3", run_cmds[0])
            self.assertIn("--name", run_cmds[0])
            self.assertIn("homeassistant", run_cmds[0])
            self.assertIn(
                ["systemctl", "--user", "restart", "home-assistant.service", "ha-data-bridge.service"],
                calls,
            )
            self.assertTrue(layout.hass_bin.is_file())
            self.assertTrue(os.access(layout.hass_bin, os.X_OK))
            wrapper = layout.hass_bin.read_text(encoding="utf-8")
            self.assertIn("--network=host", wrapper)
            self.assertIn("ghcr.io/home-assistant/home-assistant:2026.2.3", wrapper)
            self.assertIn(str(layout.config_dir), wrapper)
            self.assertNotIn(str(layout.hass_bin), str(layout.config_dir))
            self.assertEqual(sorted(path.name for path in layout.config_dir.iterdir()), [".HA_VERSION"])
            self.assertEqual((layout.config_dir / ".HA_VERSION").read_text(encoding="utf-8"), "2026.2.3\n")
            self.assertIn("Warnung:", message)
            self.assertIn("3.11.2", message)
            self.assertIn(">=3.13.2", message)
            self.assertIn("wurde gezogen", message)
            self.assertIn("Port 8123 antwortet", message)

    def test_existing_container_is_not_started_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), hass=False)
            calls = []

            def fake_run(args, timeout=120):
                args = [str(item) for item in args]
                calls.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["docker", "ps"]:
                    return subprocess.CompletedProcess(args, 0, "ha-core\tUp 2 hours\n", "")
                if args[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(args, 0, str(layout.config_dir) + "\n", "")
                if args[:2] in (["docker", "run"], ["docker", "pull"], ["docker", "start"]):
                    raise AssertionError("kein zweiter Container: " + " ".join(args))
                if args[:1] == ["systemctl"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError("unerwartet: " + " ".join(args))

            with patch.object(pu, "_resolve_ha_layout", lambda: layout), patch.object(
                pu, "_venv_python_version", lambda python: "3.11.2"
            ), patch.object(pu, "_requires_python", lambda version: ">=3.13.2"), patch.object(
                pu, "_run_cmd", fake_run
            ), patch.object(pu, "_poll_home_assistant", lambda *a, **k: True):
                message = pu.ensure_home_assistant()

            self.assertIn("bestehender Container ha-core", message)
            wrapper = layout.hass_bin.read_text(encoding="utf-8")
            self.assertIn("ha-core", wrapper)
            self.assertNotIn("docker run", " ".join(" ".join(cmd) for cmd in calls))

    def test_repair_failure_is_warning_and_update_completes(self):
        source = inspect.getsource(pu._download_worker)
        self.assertLess(source.index("_copy_download(DOWNLOAD_DIR"), source.index("ensure_home_assistant()"))
        self.assertLess(source.index("wait_for_crypt("), source.index("_copy_download(DOWNLOAD_DIR"))
        self.assertIn("DOWNLOAD_SCAN_SOFT_V1", source)
        self.assertIn("_download_worker", inspect.getsource(pu.start_download))
        self.assertIn("start_download", inspect.getsource(pu.pull_if_newer))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = self._layout(root / "ha", hass=False)
            download = root / "download"
            repo = root / "repo"
            repo.mkdir()
            jobs = []
            saved = {}

            def fake_clone(dest, job_key):
                dest = Path(dest)
                backend = dest / "miner-dashboard" / "backend"
                backend.mkdir(parents=True, exist_ok=True)
                (backend / "app.py").write_text("print(1)\n", encoding="utf-8")
                (dest / "standard_release.json").write_text(
                    json.dumps({"version": 21, "notes": "n"}),
                    encoding="utf-8",
                )

            def fake_run(args, timeout=120):
                args = [str(item) for item in args]
                if args[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(args, 1, "", "missing")
                if args[:2] == ["docker", "pull"]:
                    return subprocess.CompletedProcess(args, 1, "", "pull blocked in test")
                raise AssertionError("unerwartet: " + " ".join(args))

            patches = {
                "_clone_release": fake_clone,
                "DOWNLOAD_DIR": download,
                "ADMIN_WORK_DIR": root / "admin-work",
                "_ram_dir": lambda: root,
                "_cleanup_legacy_workdirs": lambda: None,
                "crypt_state": lambda repo=None: ("ready", ""),
                "wait_for_crypt": lambda *a, **k: "",
                "apply_admin_packages": lambda root: [],
                "_start_health_watch": lambda *a, **k: None,
                "_prune_rollbacks": lambda *a, **k: None,
                "REPO_ROOT": repo,
                "_write_job": lambda **fields: jobs.append(dict(fields)),
                "_ensure_soft_download_scan_file": lambda path=None: None,
                "apply_runtime_setup": lambda dest_root=None: ["ok"],
                "restart_control_services_soon": lambda: None,
                "save_state": lambda state: saved.update(state),
                "load_state": lambda: {"local_version": 1},
                "_resolve_ha_layout": lambda: layout,
                "_venv_python_version": lambda python: "3.11.2",
                "_requires_python": lambda version: ">=3.13.2",
                "_run_cmd": fake_run,
                "_poll_home_assistant": lambda *a, **k: True,
            }
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(patch.object(pu, name, value))
                pu._download_worker()

            self.assertTrue(jobs)
            done = jobs[-1]
            self.assertEqual(done.get("phase"), "done")
            self.assertIs(done.get("downloading"), False)
            self.assertIn("Version 21 geladen", done["message"])
            self.assertIn("Persönliche Daten unverändert.", done["message"])
            self.assertLess(
                done["message"].index("Persönliche Daten unverändert."),
                done["message"].index("Warnung:"),
            )
            self.assertIn("pull blocked in test", done["message"])
            self.assertIn("nicht abgebrochen", done["message"])
            self.assertEqual(saved.get("local_version"), 21)
            self.assertTrue(any("Home Assistant wird geprüft" in str(item.get("message")) for item in jobs))
            self.assertFalse((layout.config_dir / ".storage").exists())
            self.assertEqual((layout.config_dir / ".HA_VERSION").read_text(encoding="utf-8"), "2026.2.3\n")

    def test_upload_restarts_control_processes_only_after_success(self):
        source = inspect.getsource(pu._upload_worker)
        done = source.index("ist öffentlich")
        restart = source.index("restart_control_services_soon()")
        failed = source.index("Upload fehlgeschlagen")
        self.assertLess(done, restart)
        self.assertLess(restart, failed)
        download = inspect.getsource(pu._download_worker)
        self.assertLess(download.index("Version {version} geladen"), download.index("restart_control_services_soon()"))
        script = Path(__file__).resolve().parents[2] / "restart_control_services.sh"
        text = script.read_text(encoding="utf-8")
        for name in (
            "app.py",
            "scheduler.py",
            "control_algorithm_daemon.py",
            "energy_monitor/auto_control.py",
            "minerbot.py",
            "status.py",
            "heizstap.py",
            "ha-data-bridge.service",
        ):
            self.assertIn(name, text)
        self.assertNotIn("reboot", text)
