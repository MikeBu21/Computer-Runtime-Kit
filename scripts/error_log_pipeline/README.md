# piMiner error logs → Telegram (every ~14 days)

Sources: `Steuerungsalgorithmus.log`, `control_algorithm_daemon.log`  
(under `/dev/shm/minersteuerung/logs` and `~/minersteuerung/logs`).

## Schedule

| Job | When |
|---|---|
| Collect + Telegram send | **1st and 15th** of each month at **03:15** (~14 days) |
| Retry failed sends | **Daily 06:30** |

## Chat IDs

Fehlerberichte uses explicit `chat_ids` in `pi/config.json` (Michael / Mike But21: **0** only).  
When `chat_ids` is set, `telegram_chats.json` is **not** merged — Armin and other bot users still receive Tailscale/alarm traffic via minerbot, but not these error packages.

## Telegram attachment

Packages are archived locally as `.jsonl.gz`, but Telegram receives a **plain UTF-8 `.txt`** document (`sendDocument`) so it opens on a normal PC. A short caption text message is sent first.

## Install

```bash
cd ~/minersteuerung/scripts/error_log_pipeline/pi && ./install.sh
```

Buffer: `~/minersteuerung/logs/error_reports/{buffer,outbox,sent}`
