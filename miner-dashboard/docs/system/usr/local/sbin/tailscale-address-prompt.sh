#!/bin/bash
# One-time Tailscale address. Runs from the labwc desktop session, not the text console.
FILE=/home/piminer/.config/tailscale-address
if [ -s "$FILE" ]; then
  exit 0
fi
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  exit 0
fi
sleep 3
ADDR=$(zenity --entry --title="Tailscale" --text="Tailscale-Adresse" --width=420) || exit 0
ADDR=$(printf '%s' "$ADDR" | tr -d '[:space:]')
if [ -z "$ADDR" ]; then
  exit 0
fi
mkdir -p /home/piminer/.config
printf '%s\n' "$ADDR" > "$FILE"
chown piminer:piminer "$FILE"
chmod 600 "$FILE"
exit 0
