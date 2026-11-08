#!/bin/bash
# Fenster für einen eigenen Token. Abbrechen verwendet den gespeicherten Schlüssel.
set -u
DONE=/var/lib/tailscale-firstboot.done
LOCK="${XDG_RUNTIME_DIR:-/tmp}/tailscale-token-prompt.lock"
if [ -f "$DONE" ]; then
  exit 0
fi
if [ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
  exit 0
fi
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi
if [ -f "$DONE" ]; then
  exit 0
fi
sleep 2
TOKEN=$(zenity --entry \
  --title="Tailscale" \
  --text="Tailscale-Token. Abbrechen verwendet den gespeicherten Schlüssel." \
  --ok-label="OK" \
  --cancel-label="Abbrechen" \
  --width=480) || TOKEN=""
TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]')
if [ -z "$TOKEN" ]; then
  sudo -n /usr/local/sbin/tailscale-token-apply.sh
  exit $?
fi
printf '%s' "$TOKEN" | sudo -n /usr/local/sbin/tailscale-token-apply.sh
exit $?
