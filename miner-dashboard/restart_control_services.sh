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

exec 9>"$LOG_DIR/restart_control_services.run.lock"
if ! flock -n 9; then
  log "Neustart läuft bereits, übersprungen"
  exit 0
fi

stop_proc() {
  # Manche Prozesse (z. B. der Telegram-Bot) ignorieren SIGTERM und behalten
  # sonst den alten Code. Erst bitten, dann beenden.
  pkill -TERM -f "$PYTHON .*$1" 2>/dev/null || true
  sleep 0.4
  pkill -KILL -f "$PYTHON .*$1" 2>/dev/null || true
}

run_bg() {
  local title="$1"
  local script="$2"
  if pgrep -f "$PYTHON .*$script" >/dev/null 2>&1; then
    log "bereits neu gestartet: $title"
    return 0
  fi
  local logfile="$LOG_DIR/$(echo "$title" | tr ' /' '__').log"
  echo "[$(date -Iseconds)] Restart $title" >> "$logfile"
  (
    exec 9>&-
    cd "$BACKEND"
    nohup env MINER_RAM_CACHE=1 MINER_RAM_ROOT=/dev/shm/minersteuerung \
      MINER_RAM_FLUSH_SEC=3600 MINER_LOG_DIR="$LOG_DIR" MINER_ROOT="$ROOT" \
      "$PYTHON" $script >> "$logfile" 2>&1 &
  )
  log "gestartet: $title"
  sleep 0.25
}

# Der Wartungsprozess startet gestoppte Dienste parallel. Eine zweite Kopie beenden.
dedupe_proc() {
  local script="$1"
  local pids=()
  mapfile -t pids < <(pgrep -f "$PYTHON .*$script" || true)
  if [ "${#pids[@]}" -le 1 ]; then
    return 0
  fi
  local keep="${pids[-1]}"
  local pid
  for pid in "${pids[@]}"; do
    if [ "$pid" != "$keep" ]; then
      kill -KILL "$pid" 2>/dev/null || true
      log "Doppelten Prozess beendet: $script ($pid)"
    fi
  done
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

log "Steuerungsprozesse stoppen …"
stop_proc app.py
stop_proc scheduler.py
stop_proc control_algorithm_daemon.py
stop_proc "energy_monitor/auto_control.py"
stop_proc minerbot.py
stop_proc status.py
stop_proc heizstap.py
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

run_bg "Flask Backend" "app.py"
run_bg "Scheduler" "scheduler.py"
run_bg "Steuerungsalgorithmus" "control_algorithm_daemon.py"
run_bg "Energiemonitor" "energy_monitor/auto_control.py"
run_bg "Miner Status" "status.py"
if [ -f "$BACKEND/heizstap.py" ]; then
  run_bg "Heizstabsteuerung" "heizstap.py"
fi

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

dedupe_proc app.py
dedupe_proc scheduler.py
dedupe_proc control_algorithm_daemon.py
dedupe_proc "energy_monitor/auto_control.py"
dedupe_proc status.py
dedupe_proc heizstap.py
dedupe_proc minerbot.py

log "=== Software-Neustart fertig (kein Reboot) ==="
