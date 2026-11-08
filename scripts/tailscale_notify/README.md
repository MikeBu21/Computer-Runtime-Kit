# Tailscale notifier (piminer only)

Runs **only on piminer**. Polls every 60s and stores the current Tailscale snapshot.

**Telegram is fully disabled.** No messages are sent on IP change, startup, poll, online/offline, health, DNS, or key expiry. The service keeps running so state stays fresh; other houses get the same silence after they pull this update.

To re-enable messages later, set `TELEGRAM_SEND_ENABLED = True` in `linux/tailscale_notify.py` (and `$script:TelegramSendEnabled = $true` in the Windows script) and tighten the send rules again.

## Chat IDs (kept for a future re-enable)

Each cycle still reloads the chat list from:

1. `config.json` → `chat_ids` (optional static list)
2. **`telegram_chats.json`** written by the miner Telegram bot whenever someone `/start`s or uses a command
3. `TELEGRAM_CHAT_IDS` env (optional)

This service does **not** call Telegram `getUpdates` (that would conflict with minerbot’s long-poll).

## Install / restart (piminer)

```bash
cd ~/minersteuerung/scripts/tailscale_notify/linux
./install.sh
# after code change:
systemctl --user restart tailscale-notify.service
```

Config points at existing miner Telegram files via `token_file` / `chats_file`.
