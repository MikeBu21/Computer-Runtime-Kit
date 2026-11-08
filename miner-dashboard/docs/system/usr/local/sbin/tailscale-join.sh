#!/bin/bash
# Join Tailscale with a token from the desktop field or from SSH.
# Usage: sudo tailscale-join TOKEN
set -u
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "Aufruf: sudo tailscale-join TOKEN" >&2
  exit 1
fi
exec /usr/local/sbin/tailscale-token-apply.sh "$TOKEN"
