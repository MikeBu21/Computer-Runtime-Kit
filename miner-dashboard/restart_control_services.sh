#!/bin/bash
# Nur die Steuerungs-Programme neu starten – kein System-Reboot.
# Dienste: Scheduler, Batterie-Scraper, Flask-Backend, Energiemonitor,
# Steuerungsalgorithmus, Telegram-Bot.
set -uo pipefail

ROOT="${MINER_ROOT:-/home/piminer/minersteuerung}"
BASE="$ROOT/miner-dashboard"
BACKEND="$BASE/backend"
LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"
PYTHON="$ROOT/venv/bin/python"
mkdir -p "$LOG_DIR"

export MINER_RAM_CACHE=1
export MINER_RAM_ROOT=/dev/shm/minersteuerung
export MINER_RAM_FLUSH_SEC=3600
export MINER_LOG_DIR="$LOG_DIR"
export MINER_ROOT="$ROOT"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_DIR/restart_control_services.log"
}

stop_proc() {
  pkill -f "$PYTHON .*$1" 2>/dev/null || true
}

run_bg() {
  local title="$1"
  local script="$2"
  local logfile="$LOG_DIR/$(echo "$title" | tr ' /' '__').log"
  echo "[$(date -Iseconds)] Restart $title" >> "$logfile"
  (
    cd "$BACKEND"
    nohup env MINER_RAM_CACHE=1 MINER_RAM_ROOT=/dev/shm/minersteuerung \
      MINER_RAM_FLUSH_SEC=3600 MINER_LOG_DIR="$LOG_DIR" MINER_ROOT="$ROOT" \
      "$PYTHON" $script >> "$logfile" 2>&1 &
  )
  log "gestartet: $title"
  sleep 0.25
}

if [ ! -x "$PYTHON" ]; then
  log "FEHLER: venv fehlt: $PYTHON"
  exit 1
fi

log "=== Software-Dienste neu starten (kein Reboot) ==="

log "Batterie-Scraper (ha-data-bridge) …"
systemctl --user restart ha-data-bridge.service 2>/dev/null \
  || systemctl --user start ha-data-bridge.service 2>/dev/null \
  || log "WARNUNG: ha-data-bridge nicht neu startbar"

log "Flask-Backend / Scheduler / Energiemonitor / Steuerungsalgorithmus / Telegram stoppen …"
stop_proc app.py
stop_proc scheduler.py
stop_proc control_algorithm_daemon.py
stop_proc "energy_monitor/auto_control.py"
stop_proc minerbot.py
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

run_bg "Flask Backend" "app.py"
run_bg "Scheduler" "scheduler.py"
run_bg "Steuerungsalgorithmus" "control_algorithm_daemon.py"
run_bg "Energiemonitor" "energy_monitor/auto_control.py"

if [ -f "$BACKEND/telegram_config.json" ]; then
  TOKEN=$(python3 -c "import json; print(json.load(open('$BACKEND/telegram_config.json')).get('telegram_token','') or '')" 2>/dev/null || true)
  if [ -n "${TOKEN:-}" ]; then
    run_bg "Telegram Bot" "minerbot.py"
  else
    log "Telegram-Bot übersprungen (kein Token)"
  fi
else
  log "Telegram-Bot übersprungen (keine Config)"
fi

log "=== Software-Neustart fertig (kein Reboot) ==="
