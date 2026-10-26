#!/bin/bash
# Geordnete Auto-Start-Infrastruktur für mehrere Dienste.
# Reihenfolge unten in START_ORDER ändern – nicht die Aufrufe zerstreut anpassen.
set -uo pipefail

MODE="${1:-desktop}"   # desktop | autostart

ROOT="${MINER_ROOT:-/home/piminer/minersteuerung}"
BASE="$ROOT/miner-dashboard"
DESKTOP_START="$BASE/start_all_desktop.sh"
STATUS_TERMINAL="$BASE/open_service_status_terminal.sh"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/start-all-services.log"
CHROMIUM_LOG="$LOG_DIR/chromium-autostart.log"

# ---------------------------------------------------------------------------
# Startreihenfolge (von oben nach unten). Einträge = Funktionsnamen.
# ---------------------------------------------------------------------------
START_ORDER=(
  wait_display_step
  home_assistant
  batterie_scraper
  tailscale
  boot_repair
  miner_systemd
  miner_desktop
  open_status_terminal
  open_browsers
)

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

ensure_graphical_env() {
  export HOME="${HOME:-/home/piminer}"
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
      log "Grafik bereit (DISPLAY=${DISPLAY:-?} WAYLAND=${WAYLAND_DISPLAY:--})"
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
    # Fenster an Gesamtbildschirm anpassen (Fullscreen, kein Kiosk – Esc beendet Fullscreen)
    if chromium_running; then
      chromium --new-window --start-fullscreen "$url" >>"$CHROMIUM_LOG" 2>&1 &
    else
      chromium --start-fullscreen "$url" >>"$CHROMIUM_LOG" 2>&1 &
    fi
    sleep 4
    if chromium_running; then
      log "Chromium OK (Versuch $attempt) → $url"
      return 0
    fi
    log "WARNUNG: Chromium nicht aktiv nach Versuch $attempt ($url)"
    sleep 3
  done
  return 1
}

# ---- Schritte (von START_ORDER referenziert) --------------------------------

wait_display_step() {
  if [ "$MODE" = "autostart" ]; then
    sleep 5
    wait_for_display 60 || true
  fi
}

home_assistant() {
  log "1/… Home Assistant"
  systemctl --user start home-assistant.service 2>>"$LOG_FILE" || log "Home Assistant: Start fehlgeschlagen"
}

batterie_scraper() {
  log "2/… Batterie-Scraper (HA-Data-Bridge)"
  systemctl --user start ha-data-bridge.service 2>>"$LOG_FILE" || log "HA-Bridge: Start fehlgeschlagen"
}

tailscale() {
  log "3/… Tailscale"
  if [ -x "$HOME/bin/tailscale-start.sh" ]; then
    "$HOME/bin/tailscale-start.sh" >>"$LOG_FILE" 2>&1 || log "Tailscale: Start fehlgeschlagen"
  elif [ -x /home/piminer/bin/tailscale-start.sh ]; then
    /home/piminer/bin/tailscale-start.sh >>"$LOG_FILE" 2>&1 || log "Tailscale: Start fehlgeschlagen"
  else
    log "Tailscale: Startskript fehlt"
  fi
}

boot_repair() {
  log "4/… Boot-Reparatur / RAM-Hydrate"
  if [ -x "$BASE/boot_repair_after_powerloss.sh" ]; then
    "$BASE/boot_repair_after_powerloss.sh" >>"$LOG_FILE" 2>&1 || log "Boot-Reparatur: Warnung"
  elif [ -x "$BASE/hydrate_ram_after_powerloss.sh" ]; then
    "$BASE/hydrate_ram_after_powerloss.sh" >>"$LOG_FILE" 2>&1 || log "Hydrate: Warnung"
  fi
}

miner_systemd() {
  log "5/… systemd-User-Units (Dashboard / Maintenance)"
  systemctl --user daemon-reload 2>>"$LOG_FILE" || true
  systemctl --user enable \
    miner-boot-repair.service miner-ram-hydrate.service \
    miner-dashboard.service miner-maintenance.service 2>>"$LOG_FILE" || true
  systemctl --user start miner-boot-repair.service 2>>"$LOG_FILE" || true
  systemctl --user start miner-ram-hydrate.service 2>>"$LOG_FILE" || true
  systemctl --user start miner-dashboard.service 2>>"$LOG_FILE" || true
  systemctl --user start miner-maintenance.service 2>>"$LOG_FILE" || true
}

miner_desktop() {
  log "6/… Miner-Steuerung (Scheduler, Flask, Energiemonitor, Algo, Telegram, …)"
  if [ -x "$DESKTOP_START" ]; then
    # Keine einzelnen Log-Fenster – Status kommt im nächsten Schritt
    OPEN_MINER_BROWSER=0 SERVICE_STATUS_TERMINAL=0 "$DESKTOP_START" >>"$LOG_FILE" 2>&1
  else
    log "FEHLER: $DESKTOP_START fehlt"
    systemctl --user start miner-dashboard.service 2>>"$LOG_FILE" || true
  fi
}

open_status_terminal() {
  log "7/… Statusfenster (Dienst-Übersicht)"
  if [ "$MODE" = "autostart" ] || [ "$MODE" = "desktop" ]; then
    ensure_graphical_env
    wait_for_display 15 || true
    if [ -x "$STATUS_TERMINAL" ]; then
      "$STATUS_TERMINAL" >>"$LOG_FILE" 2>&1 || log "Statusfenster: Warnung"
    else
      log "Statusfenster-Skript fehlt: $STATUS_TERMINAL"
    fi
  fi
}

open_browsers() {
  local lan_ip miner_url ha_url miner_tries ha_tries
  lan_ip="$(get_ip 2>/dev/null || echo 127.0.0.1)"
  miner_url="http://${lan_ip}:5000"
  ha_url="http://127.0.0.1:8123"
  miner_tries=45
  ha_tries=60
  if [ "$MODE" = "autostart" ]; then
    miner_tries=60
    ha_tries=120
  fi

  wait_for_url "http://127.0.0.1:5000/" "Miner-Dashboard" "$miner_tries" || true

  if [ "$MODE" != "autostart" ] && [ "$MODE" != "desktop" ]; then
    return 0
  fi

  wait_for_display 30 || true
  log "8/… Browser: Miner-Dashboard"
  open_chromium_window "$miner_url" || log "FEHLER: Miner-Dashboard-Fenster"
  sleep 2
  if [ "$MODE" = "autostart" ]; then
    (
      wait_for_url "$ha_url/" "Home Assistant" "$ha_tries" || true
      log "Öffne Home Assistant: $ha_url"
      open_chromium_window "$ha_url" || log "FEHLER: Home-Assistant-Fenster"
    ) &
  else
    wait_for_url "$ha_url/" "Home Assistant" "$ha_tries" || true
    log "Öffne Home Assistant: $ha_url"
    open_chromium_window "$ha_url" || log "FEHLER: Home-Assistant-Fenster"
  fi
}

# ---- Hauptablauf ------------------------------------------------------------

ensure_graphical_env
log "=== Start alle Dienste (Modus: $MODE) ==="
log "Reihenfolge: ${START_ORDER[*]}"

for step in "${START_ORDER[@]}"; do
  if declare -f "$step" >/dev/null 2>&1; then
    "$step" || log "WARNUNG: Schritt $step mit Fehler beendet"
  else
    log "FEHLER: unbekannter Schritt in START_ORDER: $step"
  fi
done

log "=== Fertig ==="
{
  echo "[$(date -Iseconds)] Nachlauf-Check:"
  systemctl --user is-active miner-dashboard.service home-assistant.service ha-data-bridge.service miner-maintenance.service 2>&1 || true
  pgrep -af 'app\.py|control_algorithm_daemon|auto_control|minerbot|scheduler|ha_data_bridge' 2>&1 | head -20 || true
  pgrep -af '/usr/lib/chromium/chromium' 2>&1 | head -5 || echo "Chromium: nicht laufend"
} >>"$LOG_FILE"
