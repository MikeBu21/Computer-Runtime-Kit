#!/bin/bash
# Headless-Start aller Miner-Dashboard-Dienste (systemd / Stromausfall-Autostart).
# Pfade: /home/piminer/minersteuerung (nicht /home/piMiner/…).
set -euo pipefail

ROOT="/home/piminer/minersteuerung"
BASE="$ROOT/miner-dashboard"
BACKEND="$BASE/backend"
LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"
PYTHON="$ROOT/venv/bin/python"

mkdir -p "$LOG_DIR" "$BASE/logs"

export MINER_RAM_CACHE=1
export MINER_RAM_ROOT=/dev/shm/minersteuerung
export MINER_RAM_FLUSH_SEC=3600
export MINER_LOG_DIR=/dev/shm/minersteuerung/logs
export MINER_ROOT="$ROOT"

if [ ! -x "$PYTHON" ]; then
  echo "FEHLER: venv fehlt: $PYTHON" >&2
  exit 1
fi

stop_proc() {
  # Nur Miner-venv-Python + Script (kein blindes pkill auf den Scriptnamen allein)
  pkill -f "$PYTHON .*$1" 2>/dev/null || true
}


# Doppelstarts vermeiden
stop_proc app.py
stop_proc scheduler.py
stop_proc control_algorithm_daemon.py
stop_proc "energy_monitor/auto_control.py"
stop_proc minerbot.py
stop_proc status.py
# Watchdog läuft separat als miner-maintenance.service – hier nicht killen
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

# Nach Reboot/Stromausfall: Hydrate + JSON/Typ-Reparatur
REPAIR="$BASE/boot_repair_after_powerloss.sh"
HYDRATE="$BASE/hydrate_ram_after_powerloss.sh"
if [ -x "$REPAIR" ]; then
  "$REPAIR" || echo "WARNUNG: Boot-Reparatur fehlgeschlagen" >&2
elif [ -x "$HYDRATE" ]; then
  "$HYDRATE" || echo "WARNUNG: Hydrate fehlgeschlagen" >&2
else
  "$PYTHON" -c "from ram_cache import hydrate_after_powerloss; hydrate_after_powerloss('$BACKEND')" || true
fi

run_bg() {
  local title="$1"
  local script="$2"
  local logfile="$LOG_DIR/$(echo "$title" | tr ' /' '__').log"
  echo "[$(date -Iseconds)] Start $title" >> "$logfile"
  (
    cd "$BACKEND"
    # shellcheck disable=SC2086
    nohup env MINER_RAM_CACHE=1 MINER_RAM_ROOT=/dev/shm/minersteuerung \
      MINER_RAM_FLUSH_SEC=3600 MINER_LOG_DIR=/dev/shm/minersteuerung/logs \
      MINER_ROOT="$ROOT" \
      "$PYTHON" $script >> "$logfile" 2>&1 &
  )
  echo "  -> $title (Log: $logfile)"
  sleep 0.3
}

echo "=== Miner Dashboard starten (headless) ==="

run_bg "Flask Backend" "app.py"
run_bg "Scheduler" "scheduler.py"
run_bg "Steuerungsalgorithmus" "control_algorithm_daemon.py"
run_bg "Energiemonitor" "energy_monitor/auto_control.py"

if [ -f "$BACKEND/telegram_config.json" ] && grep -q '"telegram_token"' "$BACKEND/telegram_config.json" 2>/dev/null; then
  TOKEN=$(python3 -c "import json; print(json.load(open('$BACKEND/telegram_config.json')).get('telegram_token',''))" 2>/dev/null || true)
  if [ -n "${TOKEN:-}" ] && [ "$TOKEN" != '""' ]; then
    run_bg "Telegram Bot" "minerbot.py"
  else
    echo "  -> Telegram Bot übersprungen (Token leer)"
  fi
else
  echo "  -> Telegram Bot übersprungen (keine Config)"
fi

sleep 1
run_bg "Miner Status" "status.py"

if [ -f "$BACKEND/heizstap.py" ]; then
  run_bg "Heizstabsteuerung" "heizstap.py"
fi

echo "=== Alle Dienste gestartet ==="
