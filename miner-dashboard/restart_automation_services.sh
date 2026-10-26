#!/bin/bash
# Flask + Energiemonitor im Hintergrund neu starten (KEINE neuen Terminal-Fenster).
# Wird nur noch für Wartung/manuell genutzt – Automatik-Speichern startet Dienste nicht mehr.
set -euo pipefail

ROOT="/home/piminer/minersteuerung"
BACKEND="$ROOT/miner-dashboard/backend"
PYTHON="$ROOT/venv/bin/python"
LOG_DIR="$ROOT/logs"
DELAY="${1:-1.5}"
INCLUDE_FLASK="${2:-1}"

mkdir -p "$LOG_DIR"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

sleep "$DELAY"

stop_proc() { pkill -f "$PYTHON $1" 2>/dev/null || true; }

stop_proc "energy_monitor/auto_control.py"
if [ "$INCLUDE_FLASK" = "1" ]; then
  stop_proc app.py
  fuser -k 5000/tcp 2>/dev/null || true
fi
sleep 1

run_bg() {
  local title="$1"
  local cmd="$2"
  local log="$LOG_DIR/${title// /_}.log"
  nohup bash -c "$cmd" >> "$log" 2>&1 &
  echo "  -> Hintergrund: $title (log: $log)"
}

run_bg "Energiemonitor" "cd '$BACKEND' && '$PYTHON' energy_monitor/auto_control.py"
if [ "$INCLUDE_FLASK" = "1" ]; then
  run_bg "Flask_Backend" "cd '$BACKEND' && '$PYTHON' app.py"
fi
