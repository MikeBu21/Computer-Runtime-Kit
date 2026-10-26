
#!/bin/bash

MINER_IP="$1"
USERNAME="$2"
PASSWORD="$3"
TARGET_HASHRATE="$4"

if [[ -z "$TARGET_HASHRATE" ]]; then
  echo "❌ Ziel-Hashrate fehlt (z. B. ./control.sh IP USER PASS 100)"
  exit 1
fi

echo "🔐 Einloggen bei $MINER_IP als $USERNAME …"

# LOGIN
curl -s -X POST "http://$MINER_IP/graphql" \
  -c cookies.txt \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { auth { login(username: \\\"$USERNAME\\\", password: \\\"$PASSWORD\\\") { __typename } } }\"}"

# Optional: session_id debug
SESSION_ID=$(grep session_id cookies.txt | awk '{print $7}')
echo "✅ Session ID: $SESSION_ID"

# HASHRATE SETZEN
echo "⚙️ Setze Ziel-Hashrate auf ${TARGET_HASHRATE} TH/s …"
curl -s -X POST "http://$MINER_IP/graphql" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { bosminer { config { updateAutotuning(input: { mode: HASHRATE_TARGET, hashrateTarget: $TARGET_HASHRATE }, apply: true) { __typename ... on AutotuningError { message } } } } }\"}"

# SPEICHERN
echo "💾 Speichere Konfiguration dauerhaft …"
curl -s -X POST "http://$MINER_IP/graphql" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { bosminer { config { save { __typename } } } }"}'

# Aufräumen
rm -f cookies.txt
echo "✅ Fertig."

