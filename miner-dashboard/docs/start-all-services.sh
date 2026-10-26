#!/bin/bash
# Ein Startpunkt für Miner-Steuerung, Home Assistant und Tailscale.
set -uo pipefail

MODE="${1:-desktop}"   # desktop | autostart

ROOT="/home/piminer/minersteuerung"
DESKTOP_START="$ROOT/miner-dashboard/start_all_desktop.sh"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/start-all-services.log"
CHROMIUM_LOG="$LOG_DIR/chromium-autostart.log"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

# Grafik-Session (labwc/Wayland) – ohne XDG_RUNTIME_DIR stirbt Chromium oft still.
ensure_graphical_env() {
  export HOME="${HOME:-/home/piminer}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DISPLAY="${DISPLAY:-:0}"
  if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
    export XAUTHORITY="$HOME/.Xauthority"
  fi

  # Session-Env von labwc übernehmen (DISPLAY/WAYLAND/DBUS), falls vorhanden
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

  if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
    export WAYLAND_DISPLAY=wayland-0
  fi
  if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  fi
}

wait_for_display() {
  local tries="${1:-45}"
  local i
  ensure_graphical_env
  for ((i = 1; i <= tries; i++)); do
    ensure_graphical_env
    if [ -S "${XDG_RUNTIME_DIR}/wayland-0" ] || [ -S /tmp/.X11-unix/X0 ] || [ -S /tmp/.X11-unix/X"${DISPLAY#:}" ]; then
      log "Grafik bereit (DISPLAY=${DISPLAY:-?} WAYLAND=${WAYLAND_DISPLAY:--} XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR)"
      return 0
    fi
    sleep 2
  done
  log "WARNUNG: Kein Wayland/X11-Socket nach ${tries} Versuchen"
  return 1
}

get_ip() {
  python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("10.255.255.255", 1))
    print(s.getsockname()[0])
finally:
    s.close()
PY
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local tries="${3:-45}"
  local i code
  for ((i = 1; i <= tries; i++)); do
    # -f würde nur bei 4xx/5xx scheitern; 302 (Login-Redirect) ist OK
    code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "$url" 2>/dev/null || echo 000)"
    if [ "$code" != "000" ] && [ "$code" != "0000" ]; then
      case "$code" in
        2*|3*) log "$label bereit ($url → HTTP $code)"; return 0 ;;
      esac
    fi
    sleep 2
  done
  log "WARNUNG: $label nicht erreichbar ($url)"
  return 1
}

chromium_running() {
  pgrep -u "$(id -u)" -f '/usr/lib/chromium/chromium.*--type=zygote|/usr/lib/chromium/chromium .*http' >/dev/null 2>&1 \
    || pgrep -u "$(id -u)" -x chromium >/dev/null 2>&1 \
    || pgrep -u "$(id -u)" -f '/usr/lib/chromium/chromium' >/dev/null 2>&1
}

open_chromium_window() {
  local url="$1"
  local attempt
  ensure_graphical_env
  {
    echo "----- $(date -Iseconds) open $url -----"
    echo "DISPLAY=$DISPLAY WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  } >>"$CHROMIUM_LOG"

  if ! command -v chromium >/dev/null 2>&1; then
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$url" >>"$CHROMIUM_LOG" 2>&1 &
      return 0
    fi
    log "FEHLER: weder chromium noch xdg-open gefunden"
    return 1
  fi

  for attempt in 1 2 3; do
    ensure_graphical_env
    # Erstes Fenster: ohne --new-window (Singleton). Weitere: --new-window.
    if chromium_running; then
      chromium --new-window "$url" >>"$CHROMIUM_LOG" 2>&1 &
    else
      chromium "$url" >>"$CHROMIUM_LOG" 2>&1 &
    fi
    sleep 4
    if chromium_running; then
      log "Chromium OK (Versuch $attempt) → $url"
      return 0
    fi
    log "WARNUNG: Chromium nicht aktiv nach Versuch $attempt ($url) – siehe $CHROMIUM_LOG"
    sleep 3
  done
  return 1
}

open_home_assistant_window() {
  local url="$1"
  local tries="${2:-90}"
  log "Warte auf Home Assistant für Browser-Fenster ..."
  wait_for_url "$url/" "Home Assistant" "$tries" || true
  log "Öffne Home Assistant: $url"
  open_chromium_window "$url" || log "FEHLER: Home-Assistant-Fenster konnte nicht geöffnet werden"
}

ensure_graphical_env
log "=== Start alle Dienste (Modus: $MODE) ==="

if [ "$MODE" = "autostart" ]; then
  sleep 5
  wait_for_display 60 || true
fi

log "Starte Home Assistant ..."
systemctl --user start home-assistant.service 2>>"$LOG_FILE" || log "Home Assistant: Start fehlgeschlagen"
systemctl --user start ha-data-bridge.service 2>>"$LOG_FILE" || log "HA-Bridge: Start fehlgeschlagen"

log "Starte Tailscale ..."
if [ -x /home/piminer/bin/tailscale-start.sh ]; then
  /home/piminer/bin/tailscale-start.sh >>"$LOG_FILE" 2>&1 || log "Tailscale: Start fehlgeschlagen"
else
  log "Tailscale: tailscale-start.sh fehlt"
fi

log "Boot-Reparatur + RAM-Hydrate (nach Stromausfall) ..."
if [ -x "$ROOT/miner-dashboard/boot_repair_after_powerloss.sh" ]; then
  "$ROOT/miner-dashboard/boot_repair_after_powerloss.sh" >>"$LOG_FILE" 2>&1 || log "Boot-Reparatur: Warnung"
elif [ -x "$ROOT/miner-dashboard/hydrate_ram_after_powerloss.sh" ]; then
  "$ROOT/miner-dashboard/hydrate_ram_after_powerloss.sh" >>"$LOG_FILE" 2>&1 || log "Hydrate: Warnung"
fi
systemctl --user daemon-reload 2>>"$LOG_FILE" || true
systemctl --user enable miner-boot-repair.service miner-ram-hydrate.service miner-dashboard.service miner-maintenance.service 2>>"$LOG_FILE" || true
systemctl --user start miner-boot-repair.service 2>>"$LOG_FILE" || true
systemctl --user start miner-ram-hydrate.service 2>>"$LOG_FILE" || true
systemctl --user start miner-dashboard.service 2>>"$LOG_FILE" || true
systemctl --user start miner-maintenance.service 2>>"$LOG_FILE" || true

log "Starte Miner-Steuerung (Desktop-Wrapper / headless-aware) ..."
if [ -x "$DESKTOP_START" ]; then
  OPEN_MINER_BROWSER=0 "$DESKTOP_START" >>"$LOG_FILE" 2>&1
else
  log "FEHLER: $DESKTOP_START fehlt"
  systemctl --user start miner-dashboard.service 2>>"$LOG_FILE" || true
fi

LAN_IP="$(get_ip 2>/dev/null || echo 127.0.0.1)"
MINER_URL="http://${LAN_IP}:5000"
HA_URL="http://127.0.0.1:8123"

MINER_TRIES=45
HA_TRIES=60
if [ "$MODE" = "autostart" ]; then
  MINER_TRIES=60
  HA_TRIES=120
fi

wait_for_url "http://127.0.0.1:5000/" "Miner-Dashboard" "$MINER_TRIES" || true

if [ "$MODE" = "autostart" ] || [ "$MODE" = "desktop" ]; then
  wait_for_display 30 || true
  log "Öffne Miner-Dashboard: $MINER_URL"
  open_chromium_window "$MINER_URL" || log "FEHLER: Miner-Dashboard-Fenster konnte nicht geöffnet werden"
  sleep 2
  if [ "$MODE" = "autostart" ]; then
    open_home_assistant_window "$HA_URL" "$HA_TRIES" &
  else
    wait_for_url "$HA_URL/" "Home Assistant" "$HA_TRIES" || true
    log "Öffne Home Assistant: $HA_URL"
    open_chromium_window "$HA_URL" || log "FEHLER: Home-Assistant-Fenster konnte nicht geöffnet werden"
  fi
fi

log "=== Fertig ==="
# Kurz prüfen, ob UI-Prozesse wirklich da sind (Autostart-Diagnose)
{
  echo "[$(date -Iseconds)] Nachlauf-Check:"
  systemctl --user is-active miner-dashboard.service home-assistant.service miner-maintenance.service 2>&1 || true
  pgrep -af 'app\.py|control_algorithm_daemon|auto_control|status\.py|minerbot|scheduler' 2>&1 | head -20 || true
  pgrep -af '/usr/lib/chromium/chromium' 2>&1 | head -5 || echo "Chromium: nicht laufend"
} >>"$LOG_FILE"
