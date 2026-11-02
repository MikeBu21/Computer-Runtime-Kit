# Tailscale → Telegram notifier (piMiner only)

Runs **only on piMiner**. Polls every 60s. A Telegram message is sent only when the Tailscale IPv4 or IPv6 address changes from one real address to another. The message names the machine and the new address. The same IP does not send again: not on startup, not on the poll timer, and not when only online/offline, health, DNS, or key expiry change. The first address seen is stored and not announced.

## Chat IDs (automatic)

Each cycle reloads the full chat list from:

1. `config.json` → `chat_ids` (optional static list)
2. **`telegram_chats.json`** written by the miner Telegram bot whenever someone `/start`s or uses a command
3. `TELEGRAM_CHAT_IDS` env (optional)

New users who talk to the miner bot are added to `telegram_chats.json` by minerbot. The next notify cycle (≤60s) includes them for later alerts. Adding a chat does not send a message. IP-change alerts go to **all** known chats.

This service does **not** call Telegram `getUpdates` (that would conflict with minerbot’s long-poll).

## Install / restart (piMiner)

```bash
cd ~/minersteuerung/scripts/tailscale_notify/linux
./install.sh
# after code change:
systemctl --user restart tailscale-notify.service
```

Config points at existing miner Telegram files via `token_file` / `chats_file`.
