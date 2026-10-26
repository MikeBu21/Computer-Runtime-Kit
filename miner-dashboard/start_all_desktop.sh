#!/bin/bash
# Desktop-Start: schwarze lxterminal-Logfenster + Kern über systemd (kein Doppelstart).
set -euo pipefail

ROOT="/home/piminer/minersteuerung"
BASE="$ROOT/miner-dashboard"
BACKEND="$BASE/backend"
LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"
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
  if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
  fi
}

ensure_graphical_env
mkdir -p "$LOG_DIR"

export MINER_RAM_CACHE=1
export MINER_RAM_ROOT=/dev/shm/minersteuerung
export MINER_RAM_FLUSH_SEC=3600
export MINER_LOG_DIR=/dev/shm/minersteuerung/logs
export MINER_ROOT="$ROOT"
RAM_ENV="MINER_RAM_CACHE=1 MINER_RAM_ROOT=/dev/shm/minersteuerung MINER_RAM_FLUSH_SEC=3600 MINER_LOG_DIR=/dev/shm/minersteuerung/logs MINER_ROOT=$ROOT"

if [ ! -x "$PYTHON" ]; then
  lxterminal --no-remote --title="Miner Fehler" -e bash -c "echo 'venv fehlt: $ROOT/venv'; read -p 'Enter...'" &
  exit 1
fi

# Headless-Dienste über systemd (überleben Terminal-Schließen)
systemctl --user start miner-maintenance.service 2>/dev/null || true
systemctl --user enable miner-maintenance.service 2>/dev/null || true

REPAIR="$BASE/boot_repair_after_powerloss.sh"
HYDRATE="$BASE/hydrate_ram_after_powerloss.sh"
if [ -x "$REPAIR" ]; then
  "$REPAIR" || echo "WARNUNG: Boot-Reparatur fehlgeschlagen" >&2
elif [ -x "$HYDRATE" ]; then
  "$HYDRATE" || echo "WARNUNG: Hydrate fehlgeschlagen" >&2
else
  "$PYTHON" -c "from ram_cache import hydrate_after_powerloss; hydrate_after_powerloss('$BACKEND')" || true
fi

if ! systemctl --user is-active --quiet miner-dashboard.service 2>/dev/null; then
  systemctl --user start miner-dashboard.service 2>/dev/null || true
  # Kurz warten, bis headless-Prozesse/Logs da sind
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if pgrep -f "$PYTHON app.py" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

run_terminal() {
  local title="$1"
  local cmd="$2"
  ensure_graphical_env
  # Kein zweites Fenster mit gleichem Titel
  if pgrep -u "$(id -u)" -f "lxterminal.*--title=${title}" >/dev/null 2>&1 \
    || pgrep -u "$(id -u)" -f "lxterminal.*--title=${title// /.*}" >/dev/null 2>&1; then
    echo "  -> Terminal schon offen: $title"
    return 0
  fi
  lxterminal --no-remote --title="$title" -e bash -c "$cmd; echo; echo '[$title beendet]'; read -p 'Fenster schließen mit Enter...'" &
  echo "  -> Terminal: $title"
  sleep 0.35
}

# Log-Follower (Dienste laufen headless; Fenster nur zur Überwachung)
open_log_terminal() {
  local title="$1"
  local logfile="$2"
  touch "$logfile" 2>/dev/null || true
  run_terminal "$title" "echo '=== $title (Log: $logfile) ==='; echo 'Prozess läuft headless – Live-Log. Strg+C beendet nur die Ansicht.'; echo; tail -n 80 -F '$logfile'"
}

HEADLESS_RUNNING=0
if pgrep -f "$PYTHON app.py" >/dev/null 2>&1; then
  HEADLESS_RUNNING=1
fi

# Wenn Kern nicht läuft: headless starten (nie Prozesse IN lxterminal –
# sonst beendet start_all/Watchdog alle sechs Fenster auf einmal).
if [ "$HEADLESS_RUNNING" != "1" ]; then
  echo "=== Kern-Dienste fehlen – starte headless via start_all.sh ==="
  if [ -x "$BASE/start_all.sh" ]; then
    bash "$BASE/start_all.sh" || true
  else
    systemctl --user start miner-dashboard.service 2>/dev/null || true
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if pgrep -f "$PYTHON app.py" >/dev/null 2>&1; then
      HEADLESS_RUNNING=1
      break
    fi
    sleep 0.5
  done
fi

if [ "$HEADLESS_RUNNING" = "1" ]; then
  echo "=== Kern-Dienste laufen (headless) — öffne schwarze Log-Fenster ==="
  systemctl --user start miner-maintenance.service 2>/dev/null || true
  open_log_terminal "Flask Backend" "$LOG_DIR/Flask_Backend.log"
  open_log_terminal "Scheduler" "$LOG_DIR/Scheduler.log"
  open_log_terminal "Steuerungsalgorithmus" "$LOG_DIR/Steuerungsalgorithmus.log"
  open_log_terminal "Energiemonitor" "$LOG_DIR/Energiemonitor.log"
  if [ -f "$BACKEND/telegram_config.json" ] && grep -q '"telegram_token"' "$BACKEND/telegram_config.json" 2>/dev/null; then
    TOKEN=$(python3 -c "import json; print(json.load(open('$BACKEND/telegram_config.json')).get('telegram_token',''))" 2>/dev/null || true)
    if [ -n "${TOKEN:-}" ] && [ "$TOKEN" != '""' ]; then
      open_log_terminal "Telegram Bot" "$LOG_DIR/Telegram_Bot.log"
    fi
  fi
  open_log_terminal "Miner Status" "$LOG_DIR/Miner_Status.log"
  echo "=== Log-Terminals gestartet ==="
  exit 0
fi

echo "FEHLER: Kern-Dienste konnten nicht gestartet werden – keine Fallback-Terminals." >&2
echo "Bitte: bash $BASE/start_all.sh && bash $0" >&2
exit 1
