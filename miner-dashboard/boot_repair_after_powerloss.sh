#!/bin/bash
# Nach Stromausfall: JSON/Typ-Reparatur + RAM-Hydrate, bevor Dienste starten.
set -euo pipefail

ROOT="/home/piminer/minersteuerung"
BACKEND="$ROOT/miner-dashboard/backend"
PYTHON="$ROOT/venv/bin/python"

export MINER_RAM_CACHE="${MINER_RAM_CACHE:-1}"
export MINER_RAM_ROOT="${MINER_RAM_ROOT:-/dev/shm/minersteuerung}"
export MINER_RAM_FLUSH_SEC="${MINER_RAM_FLUSH_SEC:-3600}"
export MINER_LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"
export MINER_ROOT="$ROOT"

mkdir -p "$MINER_LOG_DIR" "$MINER_RAM_ROOT"

if [ ! -x "$PYTHON" ]; then
  echo "FEHLER: venv fehlt: $PYTHON" >&2
  exit 1
fi

# 1) RAM aus SD
if [ -x "$ROOT/miner-dashboard/hydrate_ram_after_powerloss.sh" ]; then
  "$ROOT/miner-dashboard/hydrate_ram_after_powerloss.sh" || true
fi

# 2) Reparatur / Debugging nach abrupt Stop
cd "$BACKEND"
"$PYTHON" boot_repair_after_powerloss.py
RC=$?
echo "[$(date -Iseconds)] boot_repair exit=$RC" | tee -a "$MINER_LOG_DIR/boot_repair.log"
exit 0
