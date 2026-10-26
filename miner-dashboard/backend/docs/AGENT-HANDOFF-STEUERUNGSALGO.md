# Agent-Handoff: Steuerungsalgorithmus (Pi vs. GitHub)

> **Stand:** 2026-08-30 · **Repo:** `https://github.com/MikeBu21/piMiner` (Branch `main`)  
> **Laufender Pi:** `/home/piminer/minersteuerung` · Standort Beispiel, AT  
> **Wichtig:** Stand 2026-08-30 ist der **vollständige Autopilot auf GitHub** (`origin/main`). Laufzeit-State (JSON-Heartbeat, State, Steering-History) bleibt nur auf dem Pi.

---

## 1. Git-Status auf einen Blick

| | GitHub `origin/main` | Pi (lokal) |
|---|---|---|
| Module in `control_algorithm/` | **42 Dateien** (Autopilot) | **42 Dateien** (Autopilot) |
| `service.py` | ~1560 Zeilen | ~1560 Zeilen |
| Anomalie, Min/Max-SoC-Schutz, Energiebilanz, Logikgatter | ✅ | ✅ |
| Priorität | **Skala 0–10** | **Skala 0–10** |

**Nur lokal (nicht in Git):** `control_algorithm_state.json`, `control_algorithm_heartbeat.json`, Steering-History, Energie-Zeitreihen, Logs.

---

## 2. Tick-Ablauf (`service.py` → `run_tick`)

Reihenfolge pro Daemon-Tick (~600 s):

1. Live-Daten, Wetter, Kalibrierung
2. **`min_soc_protection_actions`** → Not-Aus unter Min-SoC
3. **`refresh_min_soc_protection`** / **`sync_max_soc_protection_state`**
4. Unter Min-SoC: **keine** Bilanz, **keine** Anomalie, Budget = 0
5. Über Max-SoC: Anomalie übersprungen, **`clear_anomaly_for_max_soc_protection`**
6. `maybe_run_hourly_balance` → `energy_balance.py`
7. **`check_anomaly`** → `anomaly_detection.py` (wenn nicht Min/Max/PV-Safety/Nacht blockiert)
8. **`plan_load_actions`** → `load_scheduler.py` (Herzstück Lastplanung)
9. Runtime-Guards, Switch-Commitment, Aktionen ausführen
10. State speichern, Heartbeat

**Daemon:** `control_algorithm_daemon.py`  
**Log:** `/home/piminer/minersteuerung/logs/Steuerungsalgorithmus.log`  
**State:** `miner-dashboard/backend/control_algorithm_state.json`  
**Config:** `miner-dashboard/backend/control_algorithm_config.json`  
**Heartbeat:** `control_algorithm_heartbeat.json`

---

## 3. Autopilot-Module (auf GitHub seit 2026-08-30)

| Modul | Zweck |
|---|---|
| **`load_scheduler.py`** | Zentrale Lastplanung: pause/resume, Nacht-Sperre, Morgen-Floor, Budget, Max-SoC-Füllung |
| **`energy_balance.py`** | 7-Tage-Energiebilanz, Wochenplan, Miner-Budget kW/kWh, Snapshots |
| **`anomaly_detection.py`** | Mehr-/Unterverbrauch vs. historischer Grundverbrauch; Fast-Path pause/resume all |
| **`battery_protection.py`** | **NEU 2026-08-30:** Harte Min/Max-SoC-Grenzen, Algo-Sperre, Logikgatter-Anbindung |
| **`consumption_history.py`** | Tagesarchiv, Wochentagsprofile, Grundverbrauch-Integration, Anomalie-Basis |
| **`consumption_pattern_estimate.py`** | Tages-Pace, Verbrauchs-Override (Plan × Faktor) |
| **`device_power_measure.py`** | **Ist-kW pro Gerät** aus Hauslast-Delta lernen; Config auto-anpassen |
| **`fill_schedule.py`** | Füllkurve bis Max-SoC, `max_soc_override_active` Latch |
| **`logic_gate.py`** | UI-Graph „Logikgatter“ (Entscheidungsbaum visualisiert) |
| **`switch_commitment.py`** | Anti-Flattern, tägliches Ein-Limit, Export-/Füll-Commitments |
| **`priority_runtime.py`** | Surplus-Verteilung nach Priorität 0–10 |
| **`steering_history.py`** | Protokoll Ein/Aus inkl. Grund, offene Sessions |
| **`steering_learning.py`** | Lern-Neuronen (Tag/Nacht, Speicher-Band, Prio-Balance) |
| **`weather_survival.py`** | Autopilot-SOC-Plan, Survival, Export-Guard |
| **`pv_safety_watchdog.py`** | PV-Abweichungs-Sicherheitsabschaltung |
| **`pv_pattern_estimate.py`** | Wetter-Muster vs. OMS |
| **`curve_plan.py`** | SOC-Ladekurve steuern („mehr Last früher“) |
| **`decision_arbitrator.py`** | Schiedsrichter Budget vs. Export vs. Morgen-Floor |
| **`decision_board.py`** | Entscheidungsboard-Daten für UI |
| **`device_schedule_24h.py`** | 24h-Geräteplan |
| **`device_plan.py`** | Geräte-Einzelplanung |
| **`health_monitor.py`** | Health-Check aller Algo-Prozesse |
| **`anchor_stretch.py`** | Anker-Streckung / Morgen-Floor |
| **`storage_band_learning.py`** | Speicher-Band Lernen |
| **`peak_balance_learning.py`** | Peak-Balance Lernen |
| **`soc_priority_neuron.py`** | **NEU 2026-08-30:** Lehrneuron SoC-Priorität – checkpoint-sichere Geräteauswahl |
| **`late_start.py`** | Spätstart-Planung |
| **`soc_calibration.py`** | SOC-Kalibrierung |
| **`daemon_control.py`** | Daemon-Steuerung API |
| **`updater.py`** | Git-Update-Hilfen |
| **`paths.py`** | Pfad-Hilfen |

---

## 4. Feature-Matrix (was der User meinte)

### 4.1 Sicherheits-Abschaltung / -Einschaltung

| Mechanismus | Modul | Verhalten |
|---|---|---|
| Min-SoC Notaus | `battery_protection.py`, `load_scheduler.check_min_soc_emergency` | SoC < Min → **alle aus**, `force`+`emergency`, Algo gesperrt |
| Max-SoC Last | `battery_protection.py`, `fill_schedule.py` | SoC ≥ Max → **alle ein**, nur Resume, kein Shed |
| PV-Safety | `pv_safety_watchdog.py` | PV weicht ab → Abschaltung |
| Nacht-Sperre | `load_scheduler.night_lock_*` | Nachts pausieren (ggf. nach Min-Laufzeit) |
| Anomalie Hoch | `anomaly_detection._handle_high_anomaly` | Mehrverbrauch → **alle aus** (Fast-Path) |
| Anomalie Tief | `anomaly_detection._handle_low_anomaly` | Unterverbrauch → **alle ein** (optional) |
| Tages-Ein-Limit | `switch_commitment.py` | `max_device_on_per_day`, `hard_daily_on_limit` |
| Mindestlaufzeit | `load_scheduler.is_min_runtime_protected` | `min_device_runtime_hours` |

### 4.2 Min-SoC / Max-SoC (Batterie-Schutz)

**Config:** `min_soc_percent`, `max_soc_percent` (z. B. 35 / 95)

| Zustand | Bedingung | Algo-Rechte |
|---|---|---|
| **Min-Sperre** | SoC < Min (Freigabe ab Min+1 %) | Keine Steuerung, Budget 0, kein Resume |
| **Normal** | Min+1 % … Max−0,5 % | Volle Kontrolle |
| **Max-Schutz** | SoC ≥ Max (Ende unter Max−0,5 %) | Nur Einschalten aller Geräte, kein Abschalten |

**State-Keys:** `min_soc_lock_active`, `min_soc_lock_reason`, `max_soc_protection_active`, `max_soc_protection_reason`, `max_soc_override_active`

**Logikgatter:** Knoten `batterie_schutz` + `max_soc` in `logic_gate.py`

### 4.3 Selbstverbrauchsermittlung

- **Historie:** `consumption_history.py` – archiviert täglich, Wochentags-Mittel (`hourly_base_kwh_avg`)
- **Live:** Zeitreihe `energy_timeseries_24h.json` – `betrieb`, `miner_kw` getrennt
- **Grundverbrauch:** `max(0, betrieb − miner_kw)` je Intervall
- **Muster/Pace:** `consumption_pattern_estimate.py` – kann Plan-Verbrauch skalieren
- **Bilanz:** `energy_balance.py` – 7-Tage-Horizont, Miner-Surplus, Wochenplan

### 4.4 Tatsächlicher Geräteverbrauch (Ist-kW)

**Modul:** `device_power_measure.py`

- Misst **nicht** am Miner-API, sondern **Hauslast-Delta** bei genau einer Umschaltung
- EMA-Glättung; bei Abweichung >8 % / 0,15 kW → **`power_kw` in Config auto-update**
- State: `device_power_measure.by_ip[ip].measured_power_kw`
- Service hängt `measured_power_kw` ans Dashboard (`service._enrich_devices_measured_power`)

**Planung nutzt weiter Config-`power_kw`**; Ist-Wert ist für Lernen/Korrektur.

### 4.5 Anomalie

**Modul:** `anomaly_detection.py` + `consumption_history.corrected_base_consumption_for_anomaly`

- Vergleicht **Grundverbrauch** (ohne Miner) mit **historischem Wochentags-Schnitt**
- Config: `anomaly_threshold_percent`, `anomaly_window_hours`, `anomaly_min_history_days`, …
- Unterverbrauch optional: `anomaly_low_enabled`

**Fix 2026-08-30 (wichtig für andere Agenten):**

Problem: Algo schaltet Miner ein → Gesamtverbrauch steigt → Anomalie „Mehrverbrauch“ → Algo schaltet wieder aus.

Lösung in `consumption_history.py`:
- `algo_attributed_miner_kwh()` – erwarteter Miner-Verbrauch aus `power_kw × Laufzeit` (`device_runtime.resumed_at`)
- `corrected_base_consumption_for_anomaly()` – zieht `max(gemessener miner_kw, algo_zuschreibung)` ab
- **`check_anomaly`** übergibt jetzt `state` für Laufzeit-Erkennung

Unter Max-SoC: Anomalie wird übersprungen + aktive Hoch-Anomalie gelöscht.

### 4.6 Prioritätssortierung

**Skala 0–10** (10 = wichtigster Anker, z. B. Beispiel)

| Funktion | Datei | Logik |
|---|---|---|
| `priority_rank()` | `planner.py` | Niedrigere Zahl = höhere Prio |
| `sort_for_resume()` | `planner.py` | Höchste Prio zuerst einschalten |
| `sort_for_pause()` | `planner.py` | Niedrigste Prio zuerst ausschalten |
| `find_priority_violations()` | `planner.py` | Flex darf nicht laufen wenn Anker aus |
| `paused_devices_highest_tier()` | `planner.py` | Nächste Stufe zum Resume |
| `priority_runtime.py` | | Surplus-Budget nach Prio verteilen |

**Historisch:** GitHub hatte nur `high` / `medium` / `low` (3 Stufen) – ersetzt durch Skala 0–10.

### 4.7 Lehrneuron SoC-Priorität (`soc_priority_neuron.py`) — NEU 2026-08-30

**Zweck:** Maximal zulässige Last-Mischung nach Priorität, solange alle `soc_targets` (z. B. 89 % bis 20:00) mit Sicherheitsmarge erreichbar bleiben. Kein Neural Net – gierige Geräteauswahl + `simulate_battery_horizon`.

| Thema | Detail |
|---|---|
| **Modul** | `control_algorithm/soc_priority_neuron.py` |
| **Logikgatter** | Knoten `lehrneuron_soc_prio` (Lernband) |
| **Tick** | `service.run_tick()` → `run_soc_priority_neuron()` vor `plan_load_actions()` |
| **Integration** | `load_scheduler.py` begrenzt `priority_runtime_plan`, blockiert Resume, erzwingt Pause |
| **Lernen** | EMA-Parameter in `steering_learning.py` (`soc_prio_margin_pct`, `soc_prio_anchor_stretch`, …) |
| **Tests** | `tests/test_soc_priority_neuron.py` |

**Rollout (automatisch, kein UI-Button):**

1. **Shadow-Phase:** 14 Tage ab erstem Tick (`state.soc_priority_neuron_rollout.shadow_since`) – nur Empfehlungen, keine Umschaltungen
2. **Live-Phase:** danach automatisch (`live_since`, `auto_promoted: true`) – steuert Resume/Pause

**Config-Keys** (`control_algorithm_config.json` / Defaults in `config.py`):

```json
"soc_priority_neuron_enabled": true,
"soc_priority_neuron_shadow_days": 14,
"soc_priority_neuron_margin_pct": 5.0
```

**State-Keys:** `soc_priority_neuron`, `soc_priority_neuron_rollout`, `soc_priority_neuron_notes`

**Harte Regeln bleiben übergeordnet:** `battery_protection`, `switch_commitment`, Min/Max-SoC-Notaus.

---

## 5. Änderungen Session 2026-08-29 / 2026-08-30

1. **Stale `energy_week_plan` / 171 kWh Anzeige** – Fix in `service.py` / `energy_balance.py`: `_refresh_consumption_estimates_from_state()` auch bei Throttle-Skip
2. **Anomalie vs. eigene Miner-Einschaltung** – siehe Abschnitt 4.5
3. **`battery_protection.py`** – Min/Max-SoC harte Sperre + Logikgatter
4. **`plan_load_actions`** – Early-Return bei Min-Sperre und Max-Schutz (vor Anomalie/Nacht)
5. **Tests:** `tests/test_battery_protection.py`, erweiterte `test_consumption_history.py`
6. **`soc_priority_neuron.py`** – Lehrneuron SoC-Priorität: checkpoint-sichere Prio-Auswahl, 14 Tage Shadow → auto Live

### 5b. 2026-09-24 – Verbrauchsschätzung 120 kWh (3. Wiederholung)

**Symptom:** Karte „Verbrauch heute (geschätzt)“ ~120 kWh bei ~36 kWh PV und ~14 kWh Grundlast.

**Ursache:** `shape_hourly_miner_budgets` hat `miner_budget_kw` (oft = volle Flotte 10 kW bei Export-Vermeidung) als Mittel über PV-Stunden verteilt **ohne** Energie-Kappe, plus Phantom-Schulter `budget×0.5` in Stunden ohne PV → Summe ≈ 100+ kWh Miner.

**Fix (haltbar):**
- Stündlich: `miner ≤ Netto (PV+Hydro−Grundlast)`
- Tages-Skala: Summe ≤ Tagesüberschuss
- Guardrail `cap_planned_miner_to_day_surplus()` in `build_consumption_estimate_summary` + Week-Plan
- Regression: `test_weak_pv_day_consumption_not_inflated_by_fleet_kw`

**Formel:** `Verbrauch_mit_Minern = Grundlast + min(geplanter_Miner, max(0, PV+Hydro−Grundlast))`

---

## 6. Bekannte Fallstricke (für Debugging)

| Symptom | Ursache | Wo nachschauen |
|---|---|---|
| Miner ein → sofort aus | Anomalie Hoch (miner_kw in Zeitreihe fehlt) | `anomaly_detection.py`, Zeitreihe `miner_kw` |
| Export bei vollem Akku | Anomalie + `max_device_on_per_day` erschöpft | `steering_history.json`, State |
| 171 kWh Verbrauch Anzeige | Stale `energy_week_plan` | `energy_balance.py`, throttle in `service.py` |
| ~120 kWh Verbrauch bei wenig PV | Miner-Plan ohne Überschuss-Kappe / Phantom-Schulter | `shape_hourly_miner_budgets`, `cap_planned_miner_to_day_surplus` |
| Zwei Daemon-Instanzen | Autostart + manueller Start | `pgrep -af control_algorithm_daemon` |
| `dry_run: true` vs `false` | Test vs Live | `control_algorithm_config.json` |
| Anker läuft nicht, Flex schon | Prioritätsverletzung | `find_priority_violations` |
| Max-SoC aber Miner aus | `anomaly_active` blockierte (vor Fix) oder Daily-Limit | State, `load_scheduler` Zeile ~1225 |

---

## 7. Wichtige Config-Keys (`control_algorithm_config.json`)

```json
{
  "enabled": true,
  "dry_run": false,
  "min_soc_percent": 35,
  "max_soc_percent": 95,
  "anomaly_threshold_percent": 110,
  "anomaly_low_enabled": false,
  "max_device_on_per_day": 3,
  "hard_daily_on_limit": true,
  "min_device_runtime_hours": 1,
  "consumption_history_days": 60,
  "devices": [
    { "ip": "192.0.2.10", "power_kw": 5.5, "priority": 10, "enabled": true },
    { "ip": "192.0.2.10",  "power_kw": ..., "priority": 3,  "enabled": true }
  ]
}
```

---

## 8. Tests ausführen

```bash
cd /home/piminer/minersteuerung/miner-dashboard/backend
python -m unittest tests.test_battery_protection tests.test_control_algorithm.TestAnomalyDetection -v
python -m unittest tests.test_consumption_history.TestConsumptionHistory.test_check_anomaly_ignores_algo_miner_spike_without_miner_kw -v
```

---

## 9. Daemon neu starten (eine Instanz)

```bash
pkill -f "control_algorithm_daemon.py" || true
sleep 2
nohup bash -c 'cd /home/piminer/minersteuerung/miner-dashboard/backend && /home/piminer/minersteuerung/venv/bin/python control_algorithm_daemon.py' \
  >> /home/piminer/minersteuerung/logs/Steuerungsalgorithmus.log 2>&1 &
```

Prüfen: `control_algorithm_heartbeat.json` → `pid`, `last_tick`, `dry_run: false`

---

## 10. Dateibaum `control_algorithm/` (42 Module)

Alle Module aus Abschnitt 3 plus Basis: `__init__.py`, `calibration.py`, `config.py`, `live_data.py`, `oms_pv_forecast.py`, `planner.py`, `pv_forecast.py`, `reference_anchor.py`, `service.py`, `slot_profiles.py`, `weather.py`, `weather_scale.py`

---

## 11. Änderungskatalog Alt → Neu (für Agenten)

**Referenz Alt:** GitHub vor Commit `959230b` (12 Module, ~330 Zeilen `service.py`)  
**Referenz Neu:** Pi-Stand / `origin/main` ab `959230b` (42 Module, ~1560 Zeilen `service.py`)

### 11.1 Backend – was sich grundlegend geändert hat

| Bereich | Alt (Basis) | Neu (Autopilot) |
|---|---|---|
| Module `control_algorithm/` | 12 | **42** |
| `service.py` | PV + einfache SOC-Ziele | Voller Tick mit Schutz, Bilanz, Anomalie, Lernen |
| `load_scheduler.py` | ❌ | ✅ Zentrale Lastplanung (Herzstück) |
| `battery_protection.py` | ❌ | ✅ Harte Min/Max-SoC-Sperre |
| `anomaly_detection.py` | ❌ | ✅ Mehr-/Unterverbrauch (+ Fix 30.8.) |
| `energy_balance.py` | ❌ | ✅ 7-Tage-Bilanz, Wochenplan, Miner-Budget |
| `consumption_history.py` | ❌ | ✅ Tagesarchiv, Grundverbrauch, Anomalie-Basis |
| `device_power_measure.py` | ❌ | ✅ Ist-kW pro Gerät (Hauslast-Delta) |
| `logic_gate.py` | ❌ | ✅ Logikgatter-Graph für UI |
| `priority_runtime.py` | ❌ | ✅ Surplus nach Priorität 0–10 |
| `switch_commitment.py` | ❌ | ✅ Anti-Flattern, Tages-Ein-Limit |
| `steering_history.py` / `steering_learning.py` | ❌ | ✅ Protokoll + Lern-Neuronen |
| Priorität | `high` / `medium` / `low` (3 Stufen) | **Skala 0–10** (10 = wichtigster Anker) |
| Min/Max-SoC | Nur Config-Felder, weiche Logik | **Harte Sperre** mit Hysterese + Logikgatter |

### 11.2 Prioritäten (Geräte-Reihenfolge)

**Alt (`planner.py` + `config.py`):**
- Werte: `"high"`, `"medium"`, `"low"` (Strings)
- Sortierung: high → medium → low
- UI: 3-stufige Auswahl

**Neu:**
- Werte: **Zahl 0 … 10** (`normalize_device_priority()` in `config.py`)
- **10 = höchste Priorität** (z. B. Hydro-Anker) – zuerst einschalten, zuletzt abschalten
- **0 = niedrigste Priorität** (Flex-Last) – zuerst abschalten
- `priority_rank()` in `planner.py`: `10.0 - normalize_device_priority(prio)`
- `find_priority_violations()`: Flex darf nicht laufen, wenn höher priorisiertes Gerät pausiert ist
- `priority_runtime.py`: verteilt Surplus-Budget nach Prio
- **UI-Bereich:** Steuerungsalgorithmus → Abschnitt **„Geräte (Miner & Steckdosen)”** – pro Gerät Feld `Priorität (0–10)` + angezeigtes `measured_power_kw` (Ist-kW)
- **Dashboard + Zeitsteuerung + Telegram:** Miner/HA-Geräte werden mit `sort_miners_by_priority()` / `sort_ha_devices_by_priority()` sortiert (höchste Prio oben)

### 11.3 Batterie-Schutz (Min/Max-SoC)

**Neu seit 2026-08-30** – Modul `battery_protection.py`, angebunden in `load_scheduler.py`, `service.py`, `logic_gate.py`.

| Zustand | Bedingung | Verhalten |
|---|---|---|
| **Min-Sperre** | SoC &lt; `min_soc_percent` (Freigabe ab Min+1 %) | Alle Geräte **aus**, Budget 0, kein Resume, keine Anomalie |
| **Normal** | Min+1 % … Max−0,5 % | Volle Steuerung |
| **Max-Schutz** | SoC ≥ `max_soc_percent` (Ende unter Max−0,5 %) | Nur **Einschalten** aller Geräte, kein Abschalten |

**Config-Keys:** `min_soc_percent`, `max_soc_percent` (Standard 35 / 95)  
**State-Keys:** `min_soc_lock_active`, `min_soc_lock_reason`, `max_soc_protection_active`, `max_soc_protection_reason`, `max_soc_override_active`  
**UI-Bereich:** Steuerungsalgorithmus → **„Laststeuerung & Sicherheit”** (Felder Mindest-SoC / Max-SoC)  
**Logikgatter-Knoten:** `batterie_schutz` (Band „Batterie Min/Max”)

**Dashboard (Hauptseite `/`):** Batterie als Flow-Knoten 🔋, Liniendiagramm „Batteriestand %”, Sensoren `battery_soc` / `battery_power` in Flow-Konfiguration – **nicht** die Algo-Sperrlogik (die läuft im Daemon).

### 11.4 UI: Seite **Steuerungsalgorithmus** (`/steuerungsalgorithmus`)

Template: `templates/steuerungsalgorithmus.html` – **auf GitHub** (vollständig, ~7000 Zeilen).

**Config-Bereiche (`.section-label`, Formular links):**

| Abschnitt | Inhalt |
|---|---|
| Anlageninformationen | Standort, Speicher kWh, PV kWp, Layout |
| PV-Modell | open-meteo-solar-forecast, Ebenen Ost/West |
| Steuerungsalgorithmus-Einstellungen | enabled, dry_run, feste SoC-Zeit-Ziele |
| **Laststeuerung & Sicherheit** | **Min/Max-SoC**, Risikofaktor, Verbrauchshistorie, Anomalie-Schwelle, Nacht-Sperre, Mindestlaufzeit, Tages-Ein-Limit |
| Unterverbrauch-Anomalie | `anomaly_low_enabled`, Schwellen |
| Verbrauchs-Musterschätzer | Pace-Override (Plan × Faktor) |
| PV-Sicherheitsabschaltung | Wetter-Layer 3 (`pv_safety_watchdog`) |
| **Geräte (Miner & Steckdosen)** | IP, **Priorität 0–10**, `power_kw`, Ist-kW-Anzeige, enabled |

**Live-Panels (rechts, AJAX-Poll `/api/control_algorithm/status`):**

| Panel | Datenquelle |
|---|---|
| Erzeugungsschätzung / Verbrauchsschätzung | `energy_balance`, `consumption_history` |
| **Logikgattersteuerungsalgorithmus Autopilot** | `logic_gate.py` → SVG-Graph |
| Energie-Budget-Karussell (7 Tage) | `energy_week_plan` im State |
| **Accordion „Wochenplan (7 Tage)”** | Stündliche PV/Verbrauch/Miner/SOC-Tabelle |
| **Accordion „PV-Berechnung (Details)”** | Ebenen, 4h-Slots, Stunden-Faktoren |
| **Accordion „Grundverbrauch heute”** | Wochentags-Historie, Ist vs. Prognose |
| **Accordion „Lernen & Protokoll”** | `steering_learning` + `steering_history` |

**Logikgatter – wichtige Knoten/Bänder:**

- Live-Messung (SoC, PV, Hydro, Haus, Netz, Batterie)
- **Batterie Min/Max** → `batterie_schutz`
- Verbrauch Anomalie hoch/tief → `verbrauch_anomalie_hoch`, `verbrauch_anomalie_tief`
- Prioritäts-Laufzeit → `priority_runtime_plan` im State
- Layout-Revision: `LOGIC_GATE_LAYOUT_REV` in `logic_gate.py`

### 11.5 UI: **Dashboard** (`/`) vs. **Telegram** (`/telegrambot`)

| | Dashboard `/` | Telegram `/telegrambot` |
|---|---|---|
| Zweck | Live-Übersicht, Energie-Flow, Charts, Miner-Tabelle | Bot-Token, Alarm-Schwellen, HA-Alarm-Toggle |
| Prioritäten | Miner-Tabelle sortiert nach Prio 0–10 | Bot-Befehle nutzen `sort_miners_by_priority()` |
| Batterie | Flow-Diagramm, SOC-Chart, HA-Sensoren | Indirekt (Kelag/Netz-Alarm, kein SoC-Panel) |
| Steuerungsalgo | Nur Nav-Link zur Algo-Seite | Kein Algo-UI (Steuerung per Chat-Befehle) |
| Backend | `status.py`, `dashboard_miner_status.py`, `miner_flow_data.py` | `minerbot.py`, `telegram_*_alarm.py` |

**Navigation (`base.html` Sidebar):** Dashboard, Miner/Geräte hinzufügen, Smartmeter, Einstellungen, Dienste, Automatikmodus, Zeitsteuerung, Telegram Bot, **Steuerungsalgorithmus**, Statistik konfigurieren.

### 11.6 Was auf GitHub ist vs. was nur auf dem Pi liegt

**Auf GitHub (`origin/main`, Commit `959230b`):**
- Kompletter `control_algorithm/` (42 Module)
- `control_algorithm_daemon.py`, `control_algorithm_web.py`, `control_algorithm_config.json`
- `templates/steuerungsalgorithmus.html` (vollständiges Algo-UI)
- Alle Algo-Tests (`tests/test_*.py`, 38 Dateien)
- Integration: `app.py`, `miner_control.py`, `ha_device_control.py`, `device_registry.py`, `dashboard_miner_status.py`, …
- `AGENTS.md`, dieses Handoff-Dokument

**Nur auf dem Pi (nicht committed / Runtime – Agent darf nicht erwarten, dass es auf GitHub liegt):**

| Typ | Dateien |
|---|---|
| Laufzeit-State | `control_algorithm_state.json`, `control_algorithm_heartbeat.json`, `control_algorithm_steering_history.json` |
| Secrets | `miners.json`, `ha_devices.json`, `telegram_chats.json`, `enline_config.json` |
| Energie-Live | `energy_monitor/*.txt`, `energy_timeseries_24h.json`, `energy_stats_*.json` |
| **UI noch neuer als GitHub** | `templates/dashboard.html`, `base.html`, `telegrambot.html`, `automatik.html`, `zeitsteuerung.html` (lokale Änderungen) |
| **Neu, nur lokal** | `hashrate_timeseries.py`, `templates/add_device.html`, `templates/dienste.html`, `templates/statistik_konfigurieren.html` |

→ **Pi-Dashboard kann neuer sein als GitHub-Template.** Vor UI-Arbeit: `git status` auf dem Pi prüfen.

### 11.7 Kurz-Checkliste für neue Agenten

1. `AGENTS.md` → dieses Dokument lesen  
2. `control_algorithm_config.json` (Geräte, Prio, Min/Max-SoC)  
3. `control_algorithm_state.json` auf dem Pi (Live-Flags)  
4. `logs/Steuerungsalgorithmus.log` (letzte Ticks)  
5. UI: Steuerungsalgorithmus-Seite = alle Algo-Bereiche; Dashboard/Telegram = Übersicht/Alarme, nicht die Tick-Logik

---

*Bei Arbeit am Steuerungsalgo: zuerst dieses Dokument + `control_algorithm_state.json` + letzte Zeilen in `logs/Steuerungsalgorithmus.log` lesen.*
