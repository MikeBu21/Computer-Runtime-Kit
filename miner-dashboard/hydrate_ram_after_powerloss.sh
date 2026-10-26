#!/bin/bash
# Nach Stromausfall / hartem Abstecken: /dev/shm aus SD regenerieren.
# Kein manuelles Flask-Kommando nötig — vor Dienststart ausführen.
set -euo pipefail

ROOT="/home/piminer/minersteuerung"
BACKEND="$ROOT/miner-dashboard/backend"
PYTHON="$ROOT/venv/bin/python"
LOG_DIR="${MINER_LOG_DIR:-/dev/shm/minersteuerung/logs}"

export MINER_RAM_CACHE="${MINER_RAM_CACHE:-1}"
export MINER_RAM_ROOT="${MINER_RAM_ROOT:-/dev/shm/minersteuerung}"
export MINER_RAM_FLUSH_SEC="${MINER_RAM_FLUSH_SEC:-3600}"
export MINER_LOG_DIR="$LOG_DIR"
export MINER_ROOT="$ROOT"

mkdir -p "$LOG_DIR" "$MINER_RAM_ROOT"

if [ ! -x "$PYTHON" ]; then
  echo "FEHLER: venv fehlt: $PYTHON" >&2
  exit 1
fi

cd "$BACKEND"
"$PYTHON" - <<'PY'
import os
from pathlib import Path
from ram_cache import hydrate_after_powerloss, is_enabled, RAM_ROOT

base = str(Path(".").resolve())
print(f"[hydrate] enabled={is_enabled()} root={RAM_ROOT}")
hydrate_after_powerloss(base)
marker = RAM_ROOT / ".hydrated"
print(f"[hydrate] done marker={marker.is_file()} files={len(list(RAM_ROOT.rglob('*')))}")
PY

echo "[$(date -Iseconds)] RAM-Hydrate nach Stromausfall OK" | tee -a "$LOG_DIR/hydrate_powerloss.log"
