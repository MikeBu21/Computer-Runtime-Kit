#!/usr/bin/env bash
# Fehlerberichte → Telegram als stiller Hintergrunddienst (systemd user service).
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

# Old weekly / central-PC units and the former 14-day + daily-retry timers
systemctl --user disable --now error-log-viewer.service 2>/dev/null || true
systemctl --user disable --now error-log-collect.timer error-log-send.timer error-log-transfer.timer 2>/dev/null || true
rm -f "$UNIT_DIR/error-log-viewer.service" \
  "$UNIT_DIR/error-log-collect.timer" \
  "$UNIT_DIR/error-log-send.timer" \
  "$UNIT_DIR/error-log-transfer.service" "$UNIT_DIR/error-log-transfer.timer" \
  "$UNIT_DIR/default.target.wants/error-log-viewer.service" \
  "$UNIT_DIR/timers.target.wants/error-log-collect.timer" \
  "$UNIT_DIR/timers.target.wants/error-log-send.timer" \
  "$UNIT_DIR/timers.target.wants/error-log-transfer.timer" 2>/dev/null || true

# Manual one-shot collect (the daemon collects every 14 days by itself)
cat > "$UNIT_DIR/error-log-collect.service" <<EOF
[Unit]
Description=Collect 14-day piminer control errors into the outbox (manual)

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=ERROR_LOG_PIPELINE_CONFIG=$CFG
ExecStart=/usr/bin/python3 $ROOT/collect_errors.py
EOF

cat > "$UNIT_DIR/error-log-send.service" <<EOF
[Unit]
Description=piminer Fehlerberichte → Telegram (stiller Hintergrunddienst)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=ERROR_LOG_PIPELINE_CONFIG=$CFG
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $ROOT/send_telegram.py --daemon
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now error-log-send.service
systemctl --user --no-pager status error-log-send.service | head -5 || true
echo "Fehlerberichte: alle 14 Tage sammeln, nur mit Einträgen senden, bei Telegram-Fehler Pause + erneuter Versuch"
