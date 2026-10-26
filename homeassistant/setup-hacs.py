#!/usr/bin/env python3
"""Complete HACS setup via Home Assistant config flow."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HA_URL = "http://127.0.0.1:8123"
REFRESH_TOKEN = (
    "Beispiel"
)
CLIENT_ID = "http://127.0.0.1:8123/"


def get_token() -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        f"{HA_URL}/auth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)["access_token"]


def api(method: str, path: str, token: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{HA_URL}{path}", data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def notify(title: str, message: str) -> None:
    for cmd in (
        ["notify-send", title, message],
        ["DISPLAY=:0", "notify-send", title, message],
    ):
        try:
            env = None
            if cmd[0].startswith("DISPLAY="):
                env = {"DISPLAY": ":0", "PATH": "/usr/bin:/bin"}
                cmd = cmd[1:]
            subprocess.run(cmd, check=False, env=env, timeout=5)
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


def open_browser(url: str) -> None:
    for attempt in (
        ["DISPLAY=:0", "chromium", "--new-window", url],
        ["chromium", "--new-window", url],
        ["xdg-open", url],
    ):
        try:
            if attempt[0].startswith("DISPLAY="):
                env = {"DISPLAY": ":0", "PATH": "/usr/bin:/bin"}
                subprocess.Popen(attempt[1:], env=env, start_new_session=True)
            else:
                subprocess.Popen(attempt, start_new_session=True)
            return
        except FileNotFoundError:
            continue


def main() -> int:
    token = get_token()
    status, resp = api(
        "POST",
        "/api/config/config_entries/flow",
        token,
        {"handler": "hacs", "show_advanced_options": True},
    )
    if status != 200:
        print(f"Flow create failed: {status} {resp}")
        return 1

    flow_id = resp["flow_id"]
    status, resp = api(
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        token,
        {
            "acc_logs": True,
            "acc_addons": True,
            "acc_untested": True,
            "acc_disable": True,
        },
    )
    if status != 200:
        print(f"Flow submit failed: {status} {resp}")
        return 1

    code = resp.get("description_placeholders", {}).get("code", "")
    url = resp.get("description_placeholders", {}).get(
        "url", "https://github.com/login/device"
    )
    print(f"GitHub-Geraetecode: {code}")
    print(f"URL: {url}")
    notify("HACS Setup", f"GitHub-Code: {code}\nBitte auf github.com/login/device bestaetigen.")
    open_browser(url)

    for attempt in range(120):
        time.sleep(5)
        status, resp = api("GET", f"/api/config/config_entries/flow/{flow_id}", token)
        if status != 200:
            print(f"Poll failed: {status} {resp}")
            continue

        flow_type = resp.get("type")
        if flow_type == "create_entry":
            print("HACS erfolgreich eingerichtet!")
            notify("HACS", "Einrichtung abgeschlossen.")
            return 0
        if flow_type == "abort":
            print(f"Abgebrochen: {resp}")
            return 1
        if flow_type == "progress":
            code = resp.get("description_placeholders", {}).get("code", code)
            if attempt % 6 == 0:
                print(f"Warte auf GitHub-Bestaetigung... Code: {code}")
        else:
            print(json.dumps(resp, indent=2))

    print("Timeout: GitHub-Bestaetigung nicht erhalten.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
