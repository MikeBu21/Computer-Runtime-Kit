#!/usr/bin/env bash
# 14-day error collect → Telegram to all known chats (systemd user timers).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CFG="$ROOT/config.json"
mkdir -p "$UNIT_DIR" \
  "$HOME/minersteuerung/logs/error_reports/buffer" \
  "$HOME/minersteuerung/logs/error_reports/outbox" \
  "$HOME/minersteuerung/logs/error_reports/sent"

chmod +x "$ROOT"/collect_errors.py "$ROOT"/send_telegram.py "$ROOT"/install.sh
[[ -f "$CFG" ]] || cp "$ROOT/config.json.example" "$CFG"

# Disable old weekly / central-PC units if present
systemctl --user disable --now error-log-viewer.service 2>/dev/null || true
systemctl --user disable --now error-log-collect.timer error-log-transfer.timer 2>/dev/null || true
rm -f "$UNIT_DIR/error-log-viewer.service" \
  "$UNIT_DIR/error-log-collect.service" "$UNIT_DIR/error-log-collect.timer" \
  "$UNIT_DIR/error-log-transfer.service" "$UNIT_DIR/error-log-transfer.timer" \
  "$UNIT_DIR/default.target.wants/error-log-viewer.service" \
  "$UNIT_DIR/timers.target.wants/error-log-collect.timer" \
  "$UNIT_DIR/timers.target.wants/error-log-transfer.timer" 2>/dev/null || true

# Collect + Telegram send every 14 days at 03:15
cat > "$UNIT_DIR/error-log-collect.service" <<EOF
[Unit]
Description=Collect 14-day piMiner control errors and send via Telegram
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=ERROR_LOG_PIPELINE_CONFIG=$CFG
ExecStart=/usr/bin/python3 $ROOT/collect_errors.py
ExecStartPost=/usr/bin/python3 $ROOT/send_telegram.py
EOF

cat > "$UNIT_DIR/error-log-collect.timer" <<EOF
[Unit]
Description=piMiner error-log → Telegram every 14 days (03:15)

[Timer]
OnCalendar=*-*-01,15 03:15:00
Persistent=true
Unit=error-log-collect.service

[Install]
WantedBy=timers.target
EOF

# Daily retry for outbox if Telegram send failed
cat > "$UNIT_DIR/error-log-send.service" <<EOF
[Unit]
Description=Retry Telegram send of buffered piMiner error packages

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=ERROR_LOG_PIPELINE_CONFIG=$CFG
ExecStart=/usr/bin/python3 $ROOT/send_telegram.py
EOF

cat > "$UNIT_DIR/error-log-send.timer" <<EOF
[Unit]
Description=Daily retry error-log Telegram send (06:30)

[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true
Unit=error-log-send.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now error-log-collect.timer error-log-send.timer
echo "Timers:"
systemctl --user list-timers 'error-log-*' --no-pager || true
echo "Schedule: 1st and 15th of each month at 03:15 (~14 days), retry daily 06:30"
