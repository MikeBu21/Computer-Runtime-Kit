#!/usr/bin/env bash
# Install Tailscale→Telegram notifier for the current Linux user.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="tailscale-notify.service"
CFG="$ROOT/config.json"
STATE_DIR="$(dirname "$ROOT")/state"
mkdir -p "$UNIT_DIR" "$STATE_DIR"

if [[ ! -f "$CFG" ]]; then
  cp "$ROOT/config.json.example" "$CFG"
  chmod 600 "$CFG"
  echo "Created $CFG — fill telegram_token + chat_ids (or token_file/chats_file)."
fi

# On piMiner, default to existing minersteuerung Telegram files if still empty.
if [[ -f "$HOME/minersteuerung/miner-dashboard/backend/telegram_config.json" ]]; then
  python3 - <<PY
import json
from pathlib import Path
cfg_path = Path("$CFG")
cfg = json.loads(cfg_path.read_text())
changed = False
home = Path.home()
if not cfg.get("telegram_token") and not cfg.get("token_file"):
    cfg["token_file"] = str(home / "minersteuerung/miner-dashboard/backend/telegram_config.json")
    changed = True
if not cfg.get("chat_ids") and not cfg.get("chats_file"):
    cfg["chats_file"] = str(home / "minersteuerung/miner-dashboard/backend/telegram_chats.json")
    changed = True
if changed:
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("Linked existing minersteuerung Telegram token/chats into config.")
PY
fi

cat > "$UNIT_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=Tailscale change → Telegram notifier
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=TAILSCALE_NOTIFY_CONFIG=$CFG
Environment=TAILSCALE_NOTIFY_STATE=$STATE_DIR/state.json
ExecStart=/usr/bin/python3 $ROOT/tailscale_notify.py
Restart=on-failure
RestartSec=20

[Install]
WantedBy=default.target
EOF

chmod +x "$ROOT/tailscale_notify.py" "$ROOT/install.sh"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
/usr/bin/python3 "$ROOT/tailscale_notify.py" --config "$CFG" --state "$STATE_DIR/state.json" --force || true
echo "Installed: systemctl --user status $UNIT_NAME"
