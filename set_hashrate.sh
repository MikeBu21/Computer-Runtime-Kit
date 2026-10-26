#!/bin/bash

# Erwartet Parameter: ip, username, password, target_hashrate
MINER_IP="$1"
USERNAME="$2"
PASSWORD="$3"
TARGET_HASHRATE="$4"

if [[ -z "$MINER_IP" || -z "$USERNAME" || -z "$PASSWORD" || -z "$TARGET_HASHRATE" ]]; then
  echo "❌ Benutzung: ./set_hashrate.sh <IP> <Benutzername> <Passwort> <Hashrate z.B. 100>"
  exit 1
fi

# Schritt 1: Login und Session-ID holen
LOGIN_RESPONSE=$(curl -s -i -X POST http://$MINER_IP/graphql \
  -H "Content-Type: application/json" \
  -d '{"operationName":"Login","query":"mutation Login($username: String!, $password: String!) { auth { login(username: $username, password: $password) { __typename } } }","variables":{"username":"'"$USERNAME"'","password":"'"$PASSWORD"'"}}')

SESSION_ID=$(echo "$LOGIN_RESPONSE" | grep -i "set-cookie" | sed -n 's/.*session_id=\([^;]*\).*/\1/p')

if [ -z "$SESSION_ID" ]; then
  echo "❌ Login fehlgeschlagen oder session_id nicht gefunden."
  echo "$LOGIN_RESPONSE"
  exit 1
fi

# Schritt 2: Hashrate setzen
RESPONSE=$(curl -s -X POST http://$MINER_IP/graphql \
  -H "Content-Type: application/json" \
  -H "Cookie: session_id=$SESSION_ID" \
  -d '{"query":"mutation { bosminer { config { updateAutotuning(input: { mode: HASHRATE_TARGET, hashrateTarget: '"$TARGET_HASHRATE"' }, apply: true) { __typename } } } }"}')

if echo "$RESPONSE" | grep -q '__typename'; then
  echo "✅ Hashrate auf $TARGET_HASHRATE TH/s gesetzt ($MINER_IP)"
else
  echo "❌ Fehler bei Hashrate-Set ($MINER_IP):"
  echo "$RESPONSE"
fi

