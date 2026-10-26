#!/bin/bash
# Ein Statusfenster: zeigt, ob die Kern-Dienste laufen (kein Start, nur Anzeige).
# Liste ohne Überwachungsbot (status.py).
set -uo pipefail

ROOT="${MINER_ROOT:-/home/piminer/minersteuerung}"
BASE="$ROOT/miner-dashboard"
BACKEND="$BASE/backend"
PYTHON="$ROOT/venv/bin/python"
export HOME="${HOME:-/home/piminer}"

ensure_graphical_env() {
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DISPLAY="${DISPLAY:-:0}"
  if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
    export XAUTHORITY="$HOME/.Xauthority"
  fi
  local labwc_pid
  labwc_pid="$(pgrep -n -u "$(id -u)" labwc 2>/dev/null || true)"
  if [ -n "$labwc_pid" ] && [ -r "/proc/$labwc_pid/environ" ]; then
    local kv
    while IFS= read -r -d "" kv; do
      case "$kv" in
        DISPLAY=*|WAYLAND_DISPLAY=*|XDG_RUNTIME_DIR=*|XAUTHORITY=*|DBUS_SESSION_BUS_ADDRESS=*)
          export "$kv"
          ;;
      esac
    done < "/proc/$labwc_pid/environ"
  fi
  if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -S "${XDG_RUNTIME_DIR}/wayland-0" ]; then
    export WAYLAND_DISPLAY=wayland-0
  fi
}

proc_ok() {
  local pattern="$1"
  pgrep -u "$(id -u)" -f "$pattern" >/dev/null 2>&1
}

status_line() {
  local label="$1"
  shift
  local pat
  for pat in "$@"; do
    if proc_ok "$pat"; then
      printf "  [OK]   %-22s  läuft\n" "$label"
      return 0
    fi
  done
  printf "  [--]   %-22s  nicht aktiv\n" "$label"
}

telegram_configured() {
  [ -f "$BACKEND/telegram_config.json" ] || return 1
  python3 -c "
import json
p='$BACKEND/telegram_config.json'
t=json.load(open(p)).get('telegram_token') or ''
raise SystemExit(0 if t.strip() else 1)
" 2>/dev/null
}

render_status() {
  clear
  echo "=== Dienst-Status ($(date '+%H:%M:%S')) ==="
  echo "Aktualisiert alle 3 s. Fenster schließen: Strg+C oder Enter."
  echo
  status_line "Scheduler" "$PYTHON scheduler.py" "scheduler.py"
  # Batteriedaten laufen über HA-Bridge (Nachfolger des alten scraper.py)
  status_line "Batterie-Scraper" "ha_data_bridge.py" "scraper.py"
  status_line "Flask-Backend" "$PYTHON app.py" "venv/bin/python app.py"
  status_line "Energiemonitor" "energy_monitor/auto_control.py"
  status_line "Steuerungsalgorithmus" "control_algorithm_daemon.py"
  if telegram_configured; then
    status_line "Telegram-Bot" "$PYTHON minerbot.py" "minerbot.py"
  else
    printf "  [..]   %-22s  nicht konfiguriert\n" "Telegram-Bot"
  fi
  echo
  echo "Hinweis: Dienste starten über Auto-Start / start-all-services – dieses Fenster zeigt nur den Status."
}

# Als Watch-Schleife im gleichen Terminal (lxterminal -e …)
if [ "${1:-}" = "watch" ]; then
  trap 'exit 0' INT TERM
  while true; do
    render_status
    # Enter beendet; Timeout hält die Schleife am Laufen
    if read -r -t 3 _; then
      exit 0
    fi
  done
fi

ensure_graphical_env
TITLE="Dienst-Status"

if pgrep -u "$(id -u)" -f "lxterminal.*--title=${TITLE}" >/dev/null 2>&1; then
  echo "Statusfenster ist bereits offen."
  exit 0
fi

if ! command -v lxterminal >/dev/null 2>&1; then
  echo "lxterminal fehlt – Statusfenster nicht geöffnet."
  exit 0
fi

SCRIPT="$(readlink -f "$0" 2>/dev/null || echo "$0")"
lxterminal --no-remote --title="$TITLE" -e bash -c "'$SCRIPT' watch; echo; read -p 'Fenster schließen mit Enter...'" &
echo "Statusfenster geöffnet: $TITLE"
