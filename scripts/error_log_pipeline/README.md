# piminer Fehlerberichte → Telegram (alle ~14 Tage)

## Was erfasst wird (`pi/fault_scan.py`)

| Quelle | Inhalt |
|---|---|
| Programm-Logs | alle `*.log` in `/dev/shm/minersteuerung/logs` und `~/minersteuerung/logs`: Fehler-/Warnzeilen, Python-Tracebacks (mit Ausnahme + Code-Stelle), HTTP-5xx, Geräte mit `Status: error` |
| Systemjournal | alles ab Priorität *warning* inkl. Kernel: fehlgeschlagene Dienste, Datenträger-/Dateisystemfehler (auch gocryptfs, Datei wird per Inode ermittelt), OOM, Segfaults, Netzwerk |
| Systemzustand | Unterspannung/Drosselung (`vcgencmd get_throttled`), CPU-Temperatur, Speicherplatz, RAM, `systemctl --failed`, Coredumps, Neustarts |

Gleichartige Meldungen werden per Fingerprint gruppiert (Anzahl, erstes/letztes Auftreten).
Bekannte harmlose Meldungen werden gefiltert, aber im Bericht unter „Gefiltert“ gezählt.

## Ablauf

| Schritt | Wann |
|---|---|
| Erfassen (inkrementell, Offsets + Journal-Cursor) | stündlich (`scan_interval_sec`), damit RAM-Logs vor einem Neustart gelesen sind |
| Bericht packen + senden | alle `window_days` (14) Tage, **nur wenn mindestens `min_send_severity` (Standard: warning) vorliegt** |
| Nachversuch | bei Telegram-Fehler Pause mit Backoff |

Vorschau ohne Senden: Dashboard → Sonstiges → Fehlerbericht → „Vorschau des Berichts“
oder `python3 pi/fault_scan.py --preview`.

Optionale Config-Schlüssel: `scan_interval_sec`, `journal`, `journal_max_priority`,
`min_send_severity` (`warning`/`error`/`critical`), `ignore_patterns` (Regex-Liste).

## Chat IDs

Fehlerberichte uses explicit `chat_ids` in `pi/config.json` (Michael / Mike But21: **0** only).  
When `chat_ids` is set, `telegram_chats.json` is **not** merged — Armin and other bot users still receive Tailscale/alarm traffic via minerbot, but not these error packages.

## Telegram attachment

Packages are archived locally as `.jsonl.gz`, but Telegram receives a **plain UTF-8 `.txt`** document (`sendDocument`) so it opens on a normal PC. A short caption with severity counts and the top 3 findings is sent first.

## Install

```bash
cd ~/minersteuerung/scripts/error_log_pipeline/pi && ./install.sh
```

Buffer: `~/minersteuerung/logs/error_reports/{buffer,outbox,sent}`, Erfassungsstand: `findings.json`
