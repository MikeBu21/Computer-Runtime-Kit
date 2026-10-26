# Agent-Hinweise (piMiner / Miner-Steuerung)

**Pfad Pi:** `/home/piminer/minersteuerung`  
**GitHub:** https://github.com/MikeBu21/piMiner (Branch `main`, Commit `959230b`+)

## Steuerungsalgorithmus – zuerst lesen

Quellcode ist auf GitHub synchron; **Laufzeit-State nur lokal**. Vor Änderungen am Autopilot:

→ **[miner-dashboard/backend/docs/AGENT-HANDOFF-STEUERUNGSALGO.md](miner-dashboard/backend/docs/AGENT-HANDOFF-STEUERUNGSALGO.md)**

Dort: **Abschnitt 11 = vollständiger Änderungskatalog** (Alt 12 Module → Neu 42 Module), UI-Bereiche, Prioritäten, Batterie-Schutz, Dashboard vs. Telegram, GitHub vs. Pi.

## Was ist neu gegenüber alter GitHub-Basis?

| Thema | Alt (vor 30.8.2026) | Neu (jetzt auf GitHub) |
|---|---|---|
| Module `control_algorithm/` | 12 | **42** |
| Priorität | `high` / `medium` / `low` | **0–10** (10 = Anker, zuerst ein) |
| Min/Max-SoC | weiche Config | **`battery_protection.py`** – harte Sperre |
| Anomalie | ❌ | ✅ + Miner-Zuschreibungs-Fix (30.8.) |
| Ist-kW pro Gerät | ❌ | ✅ `device_power_measure.py` |
| Logikgatter-UI | ❌ | ✅ SVG-Graph auf `/steuerungsalgorithmus` |
| Energiebilanz / Wochenplan | ❌ | ✅ `energy_balance.py` |
| Lernen & Protokoll | ❌ | ✅ `steering_learning` + `steering_history` |
| Lehrneuron SoC-Priorität | ❌ | ✅ `soc_priority_neuron.py` (14d Shadow → auto Live) |
| MAC/IP-Recovery | ❌ | ✅ `ip_recovery.py` — DHCP-Wechsel automatisch |

## UI-Bereiche (wo was liegt)

| Seite | Route | Wichtige Bereiche |
|---|---|---|
| **Steuerungsalgorithmus** | `/steuerungsalgorithmus` | Config: Laststeuerung & Sicherheit (Min/Max-SoC), **Geräte Priorität 0–10**, Logikgatter, Wochenplan-Accordion, Lernen & Protokoll |
| **Dashboard** | `/` | Energie-Flow, Batterie-Chart, Miner-Tabelle (nach Prio sortiert), Gesamthashrate-Chart *(Pi kann neuer sein als GitHub)* |
| **Telegram Bot** | `/telegrambot` | Token, Kelag/Netz-Alarm, HA-Alarm-Toggle |
| **Zeitsteuerung** | `/zeitsteuerung` | Miner + HA-Geräte nach Priorität |
| **Automatikmodus** | `/automatikmodus` | kW/SOC-Grenzen pro Gerät (parallel zum Algo) |

## Wichtige Pfade

- Config: `miner-dashboard/backend/control_algorithm_config.json`
- State (nur Pi): `miner-dashboard/backend/control_algorithm_state.json`
- Heartbeat (nur Pi): `miner-dashboard/backend/control_algorithm_heartbeat.json`
- Log: `logs/Steuerungsalgorithmus.log`
- Daemon: `miner-dashboard/backend/control_algorithm_daemon.py`
- **MAC/IP-Recovery:** `miner-dashboard/backend/ip_recovery.py` → siehe [AGENT-MAC-IP-RECOVERY.md](miner-dashboard/backend/docs/AGENT-MAC-IP-RECOVERY.md)

## GitHub vs. Pi – Achtung

| | GitHub | Nur Pi |
|---|---|---|
| Algo-Quellcode (42 Module) | ✅ | ✅ |
| `steuerungsalgorithmus.html` | ✅ | ✅ |
| `dashboard.html`, `base.html`, `telegrambot.html` | ⚠️ kann veraltet sein | oft neuer (uncommitted) |
| State, Heartbeat, Energie-Live | ❌ | ✅ |
| Secrets (`miners.json`, `telegram_chats.json`) | ❌ | ✅ |

## Commits

Nur committen/pushen wenn der User es **explizit** verlangt.
