import json
import socket
import time
import paramiko
import os
from datetime import datetime

INPUT_FILE = "miners.json"
OUTPUT_FILE = "miner_status.json"
POLL_INTERVAL = 10  # Sekunden
HASHRATE_TIMEOUT = 2.5
# Unerreichbare Miner nicht alle 10s per SSH hämmern (CPU/Logs)
_SSH_FAIL_UNTIL = {}
_SSH_COOLDOWN_SEC = 120


def _dedupe_miners(miners):
    """Gleiche IP nur einmal (letzter Eintrag gewinnt)."""
    seen = {}
    for miner in miners or []:
        if not isinstance(miner, dict):
            continue
        ip = str(miner.get("ip") or "").strip()
        if ip:
            seen[ip] = miner
    return list(seen.values())


def read_miners():
    if not os.path.exists(INPUT_FILE):
        print(f"[Fehler] Datei '{INPUT_FILE}' fehlt.")
        return []
    try:
        with open(INPUT_FILE, "r") as f:
            miners = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[Fehler] Ungültiges JSON in '{INPUT_FILE}': {e}")
        return []
    if not isinstance(miners, list):
        print(f"[Fehler] '{INPUT_FILE}' muss eine JSON-Liste sein.")
        return []
    deduped = _dedupe_miners(miners)
    if len(deduped) != len(miners):
        print(
            f"[Warnung] Doppelte IPs in '{INPUT_FILE}' – "
            f"verwende {len(deduped)} eindeutige Miner."
        )
    return deduped


def _cgminer_summary(ip, timeout=HASHRATE_TIMEOUT):
    """Braiins/cgminer API Port 4028 – SUMMARY lesen."""
    raw = b""
    try:
        with socket.create_connection((ip, 4028), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"command":"summary"}\n')
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                raw += chunk
                if b"\x00" in chunk or len(raw) > 65536:
                    break
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace").split("\x00", 1)[0].strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            cleaned = text.rstrip(", \n\r\t")
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def parse_hashrate_ths(summary_payload):
    """GHS/MHS aus SUMMARY → TH/s (float) oder None."""
    if not isinstance(summary_payload, dict):
        return None
    rows = summary_payload.get("SUMMARY") or summary_payload.get("summary") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return None
    row = rows[0] if isinstance(rows[0], dict) else {}
    for key in ("GHS 5s", "GHS av", "GHS 1m", "GHS 5m", "GHS 15m"):
        if key in row:
            try:
                ghs = float(row[key])
            except (TypeError, ValueError):
                continue
            if ghs >= 0:
                if ghs >= 1000:
                    return round(ghs / 1000.0, 2)
                return round(ghs, 2)
    for key in ("MHS 5s", "MHS av", "MHS 1m"):
        if key in row:
            try:
                mhs = float(row[key])
            except (TypeError, ValueError):
                continue
            if mhs >= 0:
                return round(mhs / 1_000_000.0, 2)
    return None


def fetch_hashrate_ths(ip):
    return parse_hashrate_ths(_cgminer_summary(ip))


def get_ssh_status(ip, username, password):
    now = time.time()
    until = float(_SSH_FAIL_UNTIL.get(ip) or 0)
    if now < until:
        return {
            "ip": ip,
            "status": "error",
            "hashrate_ths": None,
            "timestamp": datetime.now().isoformat(),
            "cooldown": True,
        }
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            ip,
            username=username,
            password=password,
            timeout=4,
            banner_timeout=4,
            auth_timeout=4,
        )

        stdin, stdout, stderr = ssh.exec_command(
            "test -f /etc/bosminer_paused && echo paused || echo running"
        )
        status = stdout.read().decode().strip()

        ssh.close()
        _SSH_FAIL_UNTIL.pop(ip, None)

        hashrate_ths = None
        if status == "running":
            hashrate_ths = fetch_hashrate_ths(ip)

        return {
            "ip": ip,
            "status": status,
            "hashrate_ths": hashrate_ths,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        _SSH_FAIL_UNTIL[ip] = time.time() + _SSH_COOLDOWN_SEC
        return {
            "ip": ip,
            "status": "error",
            "hashrate_ths": None,
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }


def write_status(data):
    deduped = []
    seen = set()
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("ip") or "").strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        deduped.append(entry)
    data = deduped
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        from ram_cache import setup_runtime_dirs, write_json_durable

        setup_runtime_dirs(base)
        write_json_durable(base, OUTPUT_FILE, data, scope="backend")
        print(f"[Info] Status gespeichert (SD+RAM) in '{OUTPUT_FILE}'")
    except Exception as e:
        try:
            with open(OUTPUT_FILE, "w") as f:
                json.dump(data, f, indent=4)
            print(f"[Info] Status gespeichert in '{OUTPUT_FILE}' (SD-only fallback)")
        except Exception as e2:
            print(f"[Fehler] Konnte '{OUTPUT_FILE}' nicht schreiben: {e2}")
        print(f"[Warnung] RAM-Status-Schreiben: {e}")
    try:
        from hashrate_timeseries import record_hashrate_sample

        ts = record_hashrate_sample(base, data)
        print(
            f"[Info] Hashrate-Zeitreihe: Live {ts.get('live_ths')} / "
            f"Soll {ts.get('target_ths')} TH/s ({len(ts.get('samples') or [])} Punkte)"
        )
    except Exception as e:
        print(f"[Warnung] Hashrate-Zeitreihe: {e}")


def monitor_loop():
    print("[Start] Monitoring läuft ... (Beende mit STRG+C)")
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            from ram_cache import setup_runtime_dirs

            setup_runtime_dirs(base)
            print("[Info] RAM-Cache hydratisiert (miner_status aus SD falls nötig)")
        except Exception as e:
            print(f"[Warnung] RAM-Hydrate: {e}")
        while True:
            try:
                from ip_recovery import ensure_all_miner_macs

                learned = ensure_all_miner_macs()
                if learned:
                    print(f"[Info] {learned} MAC-Adresse(n) automatisch ermittelt")
            except Exception as exc:
                print(f"[Warnung] MAC-Ermittlung: {exc}")

            miners = read_miners()
            results = []

            for miner in miners:
                if miner.get("enabled") is False:
                    continue
                ip = miner.get("ip")
                username = miner.get("username") or miner.get("user")
                password = miner.get("password")

                if not ip or not username or not password:
                    print(f"[Warnung] Ungültiger Miner-Eintrag: {miner}")
                    continue

                result = get_ssh_status(ip, username, password)
                if result.get("status") == "error" and not result.get("cooldown"):
                    try:
                        from ip_recovery import (
                            capture_mac_if_missing,
                            try_recover_miner,
                        )

                        capture_mac_if_missing(ip, username, password)
                        recovered = try_recover_miner(miner)
                        if recovered.success and recovered.new_ip:
                            ip = recovered.new_ip
                            miner = dict(miner)
                            miner["ip"] = ip
                            result = get_ssh_status(ip, username, password)
                    except Exception as exc:
                        print(f"[Warnung] IP-Recovery für {ip}: {exc}")
                elif result.get("status") in ("running", "paused"):
                    try:
                        from ip_recovery import (
                            capture_mac_if_missing,
                            mark_miner_reachable,
                        )

                        # Kurze Aussetzer sind normal – nach jedem Fund Streak auf 0
                        mark_miner_reachable(ip)
                        capture_mac_if_missing(ip, username, password)
                    except Exception:
                        pass

                hr = result.get("hashrate_ths")
                hr_txt = f"{hr} TH/s" if hr is not None else "—"
                print(
                    f"[{result['ip']}] Status: {result['status']} | Hashrate: {hr_txt}"
                )
                results.append(result)

            write_status(results)
            print("-" * 50)
            time.sleep(POLL_INTERVAL)

            # Täglicher Scan für fehlende Miner (alle ~10 Minuten prüfen ob fällig)
            try:
                from ip_recovery import run_daily_missing_scan

                run_daily_missing_scan()
            except Exception:
                pass

    except KeyboardInterrupt:
        print("\n[Abbruch] Überwachung beendet.")


if __name__ == "__main__":
    monitor_loop()
