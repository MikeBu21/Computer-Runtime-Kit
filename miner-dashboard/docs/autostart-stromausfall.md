# Autostart nach Stromausfall (Pi)

## Kette
1. `loginctl Linger=yes` für User `piminer`
2. User-Systemd: `miner-dashboard.service`, `miner-maintenance.service` (Vorlagen in `docs/systemd-user/`)
3. LightDM Autologin → `docs/autostart/piminer-boot.desktop` → `pi5-boot.sh` → `~/bin/start-all-services.sh` → `start_all_desktop.sh`

## Wichtige Skripte (Repo-Pfad)
- `miner-dashboard/start_all.sh` — headless (systemd)
- `miner-dashboard/start_all_desktop.sh` — Terminal-Fenster
- `miner-dashboard/pi5-boot.sh` — Desktop-Autostart-Einstieg
- `docs/start-all-services.sh` — Kopie von `~/bin/start-all-services.sh`

## Nach Restore aus Git
```bash
cp miner-dashboard/docs/systemd-user/*.service ~/.config/systemd/user/
cp miner-dashboard/docs/start-all-services.sh ~/bin/start-all-services.sh
chmod +x ~/bin/start-all-services.sh miner-dashboard/*.sh
systemctl --user daemon-reload
systemctl --user enable --now miner-maintenance.service miner-dashboard.service
```
