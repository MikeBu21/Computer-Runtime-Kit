#!/bin/bash
# Ein Statusfenster: zeigt, ob die Kern-Dienste laufen, und startet fehlende neu.
# Liste ohne Überwachungsbot (status.py).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${MINER_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BASE="$ROOT/miner-dashboard"
BACKEND="$BASE/backend"
PYTHON="$ROOT/venv/bin/python"
LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"
# Mindestabstand zwischen Neustart-Versuchen pro Dienst (Sekunden)
RESTART_COOLDOWN_SEC="${SERVICE_STATUS_RESTART_COOLDOWN_SEC:-15}"
export HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"

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

# True nur bei echtem Interpreter-Prozess – nicht bei bash/lxterminal/pgrep-Wrappern.
# Entspricht control_algorithm.health_monitor._proc_running.
proc_ok() {
  local pattern="$1"
  local cmd first base
  while IFS= read -r cmd; do
    cmd="${cmd#"${cmd%%[![:space:]]*}"}"
    [ -n "$cmd" ] || continue
    case "$cmd" in
      *"$pattern"*) ;;
      *) continue ;;
    esac
    first="${cmd%% *}"
    base="${first##*/}"
    case "$base" in
      bash|sh|dash|lxterminal|xterm|gnome-terminal|xfce4-terminal|screen|tmux|pgrep|grep|rg|less|more|tail|cat|nano|vi|vim)
        continue
        ;;
    esac
    case "$base" in
      python|python2|python3|python3.*)
        return 0
        ;;
    esac
    # venv-Symlink o. Ä.: Binary heißt „python“, Pfad endet auf /python
    case "$first" in
      */python|*/python3|*/python3.*)
        return 0
        ;;
    esac
  done < <(ps -u "$(id -u)" -o args= 2>/dev/null || true)
  return 1
}

_cooldown_file() {
  local key="$1"
  local safe
  safe="$(printf '%s' "$key" | tr -c 'A-Za-z0-9._-' '_')"
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  printf '%s' "$LOG_DIR/.status_restart_${safe}.ts"
}

_restart_allowed() {
  local key="$1"
  local f now last
  f="$(_cooldown_file "$key")"
  now="$(date +%s)"
  if [ -f "$f" ]; then
    last="$(cat "$f" 2>/dev/null || echo 0)"
    if [ -n "$last" ] && [ "$last" -eq "$last" ] 2>/dev/null; then
      if [ $((now - last)) -lt "$RESTART_COOLDOWN_SEC" ]; then
        return 1
      fi
    fi
  fi
  printf '%s' "$now" >"$f" 2>/dev/null || true
  return 0
}

_ram_env() {
  export MINER_RAM_CACHE="${MINER_RAM_CACHE:-1}"
  export MINER_RAM_ROOT="${MINER_RAM_ROOT:-/dev/shm/minersteuerung}"
  export MINER_RAM_FLUSH_SEC="${MINER_RAM_FLUSH_SEC:-3600}"
  export MINER_LOG_DIR="$LOG_DIR"
  export MINER_ROOT="$ROOT"
}

# Startet einen Backend-Python-Dienst headless (wie start_all.sh / restart_control_services.sh).
start_backend_script() {
  local label="$1"
  local script="$2"
  local logfile safe
  if [ ! -x "$PYTHON" ]; then
    return 1
  fi
  if [ ! -f "$BACKEND/$script" ]; then
    return 1
  fi
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  safe="$(printf '%s' "$label" | tr ' /' '__')"
  logfile="$LOG_DIR/${safe}.log"
  _ram_env
  {
    echo "[$(date -Iseconds)] Statusfenster: Neustart $label"
  } >>"$logfile" 2>/dev/null || true
  (
    cd "$BACKEND" || exit 1
    # shellcheck disable=SC2086
    nohup env MINER_RAM_CACHE=1 MINER_RAM_ROOT=/dev/shm/minersteuerung \
      MINER_RAM_FLUSH_SEC=3600 MINER_LOG_DIR="$LOG_DIR" MINER_ROOT="$ROOT" \
      "$PYTHON" $script >>"$logfile" 2>&1 &
  )
  return 0
}

start_ha_bridge() {
  systemctl --user start ha-data-bridge.service 2>/dev/null \
    || systemctl --user restart ha-data-bridge.service 2>/dev/null \
    || return 1
  return 0
}

# Prüfen → ggf. „läuft nicht“ anzeigen → automatisch neu starten.
ensure_service() {
  local label="$1"
  local pattern="$2"
  local kind="$3"   # backend | ha_bridge
  local script="${4:-}"

  if proc_ok "$pattern"; then
    printf "  [OK]   %-22s  läuft\n" "$label"
    return 0
  fi

  printf "  [--]   %-22s  läuft nicht\n" "$label"

  if ! _restart_allowed "$pattern"; then
    printf "         %-22s  Neustart wartet (Cooldown)\n" ""
    return 1
  fi

  printf "         %-22s  starte neu…\n" ""
  case "$kind" in
    ha_bridge)
      start_ha_bridge || true
      ;;
    backend)
      start_backend_script "$label" "$script" || true
      ;;
  esac
  sleep 0.8
  if proc_ok "$pattern"; then
    printf "  [OK]   %-22s  läuft (neu gestartet)\n" "$label"
    return 0
  fi
  printf "  [!!]   %-22s  läuft nicht\n" "$label"
  return 1
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
  echo "Aktualisiert alle 3 s. Fehlende Dienste werden automatisch neu gestartet."
  echo "Fenster schließen: Strg+C oder Enter."
  echo
  ensure_service "Scheduler" "scheduler.py" backend "scheduler.py"
  ensure_service "Batterie-Scraper" "ha_data_bridge.py" ha_bridge
  ensure_service "Flask-Backend" "app.py" backend "app.py"
  ensure_service "Energiemonitor" "energy_monitor/auto_control.py" backend "energy_monitor/auto_control.py"
  ensure_service "Steuerungsalgorithmus" "control_algorithm_daemon.py" backend "control_algorithm_daemon.py"
  if telegram_configured; then
    ensure_service "Telegram-Bot" "minerbot.py" backend "minerbot.py"
  else
    printf "  [..]   %-22s  nicht konfiguriert\n" "Telegram-Bot"
  fi
  echo
  echo "Hinweis: Gestoppte Dienste erscheinen als „läuft nicht“ und werden hier neu gestartet."
}

# Als Watch-Schleife im gleichen Terminal (lxterminal -e …)
if [ "${1:-}" = "watch" ]; then
  trap 'exit 0' INT TERM
  mkdir -p "$LOG_DIR" 2>/dev/null || true
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
