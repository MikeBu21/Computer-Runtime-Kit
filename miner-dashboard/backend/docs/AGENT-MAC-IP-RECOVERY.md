# Agent-Handoff: MAC-basierte IP-Wiederherstellung

**Feature:** Automatische IP-Wiederherstellung für Miner per MAC-Adresse  
**Kernmodul:** `miner-dashboard/backend/ip_recovery.py`  
**Seit:** August 2026

## Problem

Miner sind überall mit der **IP als Primärschlüssel** hinterlegt (`miners.json`, `control_algorithm_config.json`, Algo-State `by_ip`, `miner_status.json`, Energy-Monitor). Bei DHCP-Wechsel bricht Monitoring, Steuerung, Dashboard und Telegram ab.

**HA-Geräte** (`ha_devices.json`) nutzen stabile `entity_id`s — kein MAC-Recovery nötig.

## Architektur

```
Verbindungsfehler (status.py / miner_control / Algo)
        ↓
try_recover_miner(miner)
        ↓
resolve_ip_by_mac()  ← detect_local_subnets() + ip neigh + Ping-Sweep
        ↓
migrate_device_ip(old_ip, new_ip)  → alle JSON-Stores atomisch
        ↓
Telegram: ip_changed  |  bei Misserfolg: device_missing + Ja/Nein
```

## Recovery-States (`miners.json`)

| State | Bedeutung |
|---|---|
| `ok` | Normal erreichbar |
| `searching` | Reserviert |
| `missing` | Nicht per MAC gefunden — User-Rückfrage |
| `awaiting_user` | User bestätigt „Gerät existiert“ → täglicher Scan |
| `removed` | Reserviert |

## Neue Felder in `miners.json`

```json
{
  "ip": "192.0.2.10",
  "mac": "aa:bb:cc:dd:ee:ff",
  "mac_source": "ssh|manual|arp",
  "recovery_state": "ok",
  "recovery_updated_at": "2026-08-30T17:00:00",
  "last_seen_ip": "192.0.2.10",
  "last_recovery_scan_at": "2026-08-30T17:00:00",
  "user_confirmed_exists": false
}
```

## MAC automatisch aus IP lernen

Reihenfolge in `discover_mac_for_ip()` / `capture_mac_if_missing()`:

1. **ARP** — Ping zur IP, dann `ip neigh show <ip>` (kein SSH nötig)
2. **SSH** — `/sys/class/net/eth0/address` auf dem Miner (Fallback)

Auslöser: `status.py` (jeder Poll), Dashboard-Laden (`app.py`), Miner hinzufügen, `ensure_all_miner_macs()`, Bootstrap-Skript.

## Hook-Punkte

| Datei | Wann |
|---|---|
| `status.py` | Bei SSH-Fehler → Recovery; bei Erfolg → MAC lernen; Daily-Scan im Loop |
| `miner_control.py` | Indirekt über `device_registry.execute_miner_command` |
| `device_registry.py` | MAC-Capture + Recovery vor Retry bei pause/resume |
| `control_algorithm/service.py` | `_fresh_status()` bei SSH-Fehler |
| `scheduler.py` | Alle ~10 Min `run_daily_missing_scan()` |
| `minerbot.py` | Job `ip_recovery_notification_job` + Inline-Buttons |
| `app.py` | `/api/miner/recovery/pending`, `/api/miner/<ip>/recovery/confirm` |

## Subnet-Erkennung

Router-unabhängig via `ip -4 -o addr show scope global up` → CIDR pro Interface (z.B. `192.168.178.0/24`, `192.168.1.0/24`, `10.0.0.0/24`).

## IP-Migration aktualisiert

1. `miners.json` — `ip`, Timestamps, `recovery_state=ok`
2. `control_algorithm_config.json` — `devices[].ip`
3. `control_algorithm_state.json` — alle `by_ip`-Maps rekursiv
4. `miner_status.json`
5. `energy_monitor/control_latches.json`
6. `energy_monitor/last_actions.json`

Hilfsfunktion: `control_algorithm/config.py` → `migrate_device_key(old, new)`

## User-Flow bei fehlendem Gerät

1. Scan findet MAC nicht → `recovery_state=missing`
2. Dashboard-Banner + Telegram mit **Ja, Gerät existiert** / **Nein, entfernen**
3. **Ja** → `awaiting_user`, Scan alle 24h
4. **Nein** → `remove_miner_completely()` löscht aus allen Stores

## Bootstrap bestehender Miner

```bash
cd /home/piminer/minersteuerung
python3 scripts/bootstrap_miner_macs.py
```

Liest MAC per SSH für erreichbare Miner; setzt `missing` für nicht erreichbare.

## Rate-Limits

- Sofort-Scan: max. 1× pro Miner alle **5 Minuten**
- Täglicher Scan: alle **24 Stunden** für `missing` / `awaiting_user`

## Telegram

- Benachrichtigungs-Queue: `ip_recovery_notifications.json` (gitignored)
- Bot-Job alle 20s: `ip_recovery_notification_job`
- Callback-Pattern: `rec:y:<mac_hex>` / `rec:n:<mac_hex>`

## Testplan

1. `bootstrap_miner_macs.py` auf Pi — MACs gespeichert
2. IP in `miners.json` absichtlich falsch setzen → innerhalb ~10s Recovery via `status.py`
3. Prüfen: Config, State, Status konsistent
4. Gerät vom Netz trennen → Dashboard/Telegram-Frage
5. „Nein“ → Miner überall entfernt

## Wichtig für Agenten

- **Niemals** `miners.json` mit echten Passwörtern committen (gitignored)
- IP bleibt `device_key` im Algo — MAC ist nur Recovery-Identität
- File-Lock: `.ip_recovery.lock` bei Migration
- Bei parallelen Prozessen (status, algo, flask) immer `ip_recovery.py` nutzen, nicht manuell JSON patchen
