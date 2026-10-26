#!/bin/bash

# Nutzung: ./control.sh IP USERNAME PASSWORD HASHRATE
MINER_IP="$1"
USERNAME="$2"
PASSWORD="$3"
TARGET_HASHRATE="$4"

if [[ -z "$TARGET_HASHRATE" ]]; then
  echo "❌ Ziel-Hashrate fehlt (z. B. ./control.sh IP USER PASS 100)"
  exit 1
fi

echo "🔐 Einloggen bei $MINER_IP als $USERNAME …"

# LOGIN zur GraphQL-Session
curl -s -X POST "http://$MINER_IP/graphql" \
  -c cookies.txt \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { auth { login(username: \\\"$USERNAME\\\", password: \\\"$PASSWORD\\\") { __typename } } }\"}"

# Optional: Session-ID anzeigen
SESSION_ID=$(grep session_id cookies.txt | awk '{print $7}')
echo "✅ Session ID: $SESSION_ID"

# Ziel-Hashrate setzen (Autotuning aktivieren)
echo "⚙️ Setze Ziel-Hashrate auf ${TARGET_HASHRATE} TH/s …"
curl -s -X POST "http://$MINER_IP/graphql" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { bosminer { config { updateAutotuning(input: { mode: HASHRATE_TARGET, hashrateTarget: $TARGET_HASHRATE }, apply: true) { __typename ... on AutotuningError { message } } } } }\"}"

# Konfiguration speichern (optional; funktioniert nicht bei allen Geräten)
echo "💾 Versuche, Konfiguration zu speichern …"
curl -s -X POST "http://$MINER_IP/graphql" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { bosminer { config { save { __typename } } } }"}'

# Aufräumen
rm -f cookies.txt
echo "✅ Fertig."

