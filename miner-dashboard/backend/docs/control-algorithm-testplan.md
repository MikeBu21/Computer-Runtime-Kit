# Testplan – Steuerungsalgorithmus

## Voraussetzungen

- Python-Virtualenv: `cd backend && python3 -m venv venv && ./venv/bin/pip install -r requirements-control-algorithm.txt`
- Dienste starten mit `./venv/bin/python` (siehe `start_all.sh`)
- Flask-Dashboard läuft (`./venv/bin/python app.py` oder bestehender Dienst)
- Optional: `control_algorithm_daemon.py` als systemd-Dienst
- `miners.json` mit mindestens einem Miner
- `daten.json` mit SOC (für Speicher-Tests)

## 1. UI & Konfiguration

1. Sidebar: **Steuerungsalgorythmus** öffnet `/steuerungsalgorithmus`
2. Stadt „Wien“ speichern → keine Fehlermeldung, Koordinaten gesetzt
3. Unbekannte Stadt → Fehlermeldung beim Speichern
4. Speicher + PV Checkboxen blenden Felder ein/aus
5. SOC-Ziele mit **+** hinzufügen/entfernen, speichern, Seite neu laden → Werte erhalten
6. Miner aktivieren + kW eintragen, speichern

## 2. Status-API

```bash
curl -s http://127.0.0.1:5000/api/control_algorithm/status | python3 -m json.tool
```

Erwartung: `pv_yield_today_kwh`, `info_messages`, `eta_message`, `devices`

## 3. Testmodus (dry_run)

1. **Testmodus** angehakt, **Steuerung aktiv** angehakt, speichern
2. Daemon starten oder Status-API pollen
3. Geplante Aktionen erscheinen in Info-Panel mit `[Testmodus]`
4. Miner werden **nicht** per nc gesteuert

## 4. Live-Steuerung (Vorsicht)

1. Automatikmodus für Test-Miner **deaktivieren**
2. Testmodus **aus**, Steuerung aktiv
3. Nur mit einem Test-Miner und beobachtetem SOC testen

## 5. Kalibrierung

- Morgens (5–8 Uhr): Daemon läuft → `morning_pv_forecast_kwh` in `control_algorithm_state.json`
- Abends 23:00: `performance_ratio` und `yield_history` aktualisiert (wenn PV > 5 kWh)
- PV-Prognose über open-meteo-solar-forecast (Details in `pv_forecast_detail` in der API)

## 6. Automatik-Konflikt

- Miner mit `auto: true` und Algorithmus-Checkbox → gelber Banner im UI

## Unit-Tests

```bash
cd /home/piMiner/miner-dashboard/backend
./venv/bin/python -m pytest tests/test_control_algorithm.py tests/test_oms_pv_forecast.py -v
# oder
./venv/bin/python -m unittest tests.test_control_algorithm tests.test_oms_pv_forecast
```
