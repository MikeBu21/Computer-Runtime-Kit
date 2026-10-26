#!/bin/bash
# Autostart nach Pi-Login – ruft den zentralen Startpunkt auf.
# Wartet kurz auf labwc/Wayland, damit Chromium-Dashboards nicht still sterben.
export HOME="${HOME:-/home/piminer}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DISPLAY="${DISPLAY:-:0}"

for _ in $(seq 1 30); do
  if [ -S "$XDG_RUNTIME_DIR/wayland-0" ] || [ -S /tmp/.X11-unix/X0 ]; then
    break
  fi
  sleep 1
done

sleep 8
exec /home/piminer/bin/start-all-services.sh autostart
