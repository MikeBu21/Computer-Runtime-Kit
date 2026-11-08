#!/bin/bash
# Wait for boot-time encryption to finish. This does not start encryption.
i=0
while [ ! -f /run/miner-chip-crypt.ready ] && [ "$i" -lt 600 ]; do
  sleep 2
  i=$((i + 1))
done
# Autostart nach Pi-Login – ruft den zentralen Startpunkt auf.
# Wartet kurz auf labwc/Wayland, damit Chromium-Dashboards nicht still sterben.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${MINER_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DISPLAY="${DISPLAY:-:0}"
export MINER_ROOT="$ROOT"

for _ in $(seq 1 30); do
  if [ -S "$XDG_RUNTIME_DIR/wayland-0" ] || [ -S /tmp/.X11-unix/X0 ]; then
    break
  fi
  sleep 1
done

sleep 8
if [ -x "$ROOT/miner-dashboard/start-all-services.sh" ]; then
  exec "$ROOT/miner-dashboard/start-all-services.sh" autostart
fi
if [ -x "$HOME/bin/start-all-services.sh" ]; then
  exec "$HOME/bin/start-all-services.sh" autostart
fi
exit 1
