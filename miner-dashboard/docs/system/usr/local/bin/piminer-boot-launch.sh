#!/bin/bash
# Autostart entry outside the encrypted mining folder. That folder is mounted only
# after login, so wait for the unlock and for pi5-boot.sh, then hand over to it.
BOOT=/home/piminer/minersteuerung/miner-dashboard/pi5-boot.sh
READY=/run/miner-chip-crypt.ready
i=0
while { [ ! -f "$READY" ] || [ ! -x "$BOOT" ]; } && [ "$i" -lt 600 ]; do
  sleep 2
  i=$((i + 1))
done
if [ ! -x "$BOOT" ]; then
  logger -t piminer-boot-launch "pi5-boot.sh not reachable: $BOOT" 2>/dev/null || true
  exit 1
fi
exec "$BOOT"
