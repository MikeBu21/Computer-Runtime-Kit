#!/bin/bash
# Verbindet Tailscale. Ein eingegebener Token wird gespeichert.
# Ohne Eingabe wird der Schlüssel aus /etc/tailscale/authkey verwendet.
set -u
DONE=/var/lib/tailscale-firstboot.done
JOINED=/var/lib/tailscale-joined
KEYFILE=/etc/tailscale/authkey
LOCK=/run/tailscale-token-apply.lock
CLEARED=/run/tailscale-state-cleared
mkdir -p /run /var/lib /etc/tailscale
exec 9>"$LOCK"
flock 9
EXPLICIT=0
if [ -n "${1:-}" ]; then
  TOKEN="$1"
  EXPLICIT=1
elif [ ! -t 0 ]; then
  TOKEN="$(cat 2>/dev/null || true)"
else
  TOKEN=""
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
if [ -n "$TOKEN" ]; then
  EXPLICIT=1
fi
# Ohne neue Eingabe nichts mehr tun, sobald schon verbunden.
# Ein eingetippter Token gewinnt immer und ersetzt den gespeicherten Schlüssel dauerhaft.
if [ "$EXPLICIT" != 1 ]; then
  if [ -f "$DONE" ]; then
    exit 0
  fi
  if [ -s "$KEYFILE" ]; then
    TOKEN="$(tr -d '[:space:]' < "$KEYFILE")"
  fi
fi
if [ -z "$TOKEN" ] || ! command -v tailscale >/dev/null 2>&1; then
  echo "kein Tailscale-Schluessel" >>/var/log/tailscale-join.log
  exit 1
fi
printf '%s\n' "$TOKEN" > "$KEYFILE"
chmod 600 "$KEYFILE"
if [ ! -f "$CLEARED" ]; then
  systemctl stop tailscaled >/dev/null 2>&1 || true
  rm -rf /var/lib/tailscale
  touch "$CLEARED"
fi
systemctl enable --now tailscaled >/dev/null 2>&1 || systemctl start tailscaled >/dev/null 2>&1 || true
ok=0
i=0
while [ "$i" -lt 12 ]; do
  if tailscale up --auth-key="$TOKEN" --reset --force-reauth --timeout 25s >>/var/log/tailscale-join.log 2>&1; then
    ok=1
    break
  fi
  i=$((i + 1))
  sleep 5
done
if [ "$ok" != "1" ]; then
  echo "tailscale up failed" >>/var/log/tailscale-join.log
  exit 1
fi
touch "$DONE" "$JOINED"
chmod 600 "$DONE"
chmod 644 "$JOINED"
exit 0
