# Home Assistant (piMiner)

Snapshot of the main Pi HA install used by the mining control stack.

## Layout

| Path | Role |
|------|------|
| `config/` | `configuration.yaml`, `secrets.yaml`, automations/scripts/scenes, `.storage` (dashboards, registries, auth) |
| `config/custom_components/` | `solarman`, `byd_hvs`, `goecharger_api2`, `hacs` |
| `ha_data_bridge.py` | Polls HA API every 30s → `/tmp/ha_snapshot.json` (Miner dashboard reads this) |
| `ha-bridge/credentials.json` | Refresh token for the bridge (plaintext) |
| systemd units | `miner-dashboard/docs/systemd-user/home-assistant.service`, `ha-data-bridge.service` |

Miner-side mapping/readers (already under `minersteuerung/`):

- `config/ha_entity_mapping.json`
- `miner-dashboard/backend/ha_entity_mapping.py`
- `miner-dashboard/backend/ha_device_control.py`
- `miner-dashboard/backend/energy_monitor/ha_live_reader.py`
- `miner-dashboard/backend/dashboard_sources.py`

## Install on target Pi

Units expect `/home/piminer/homeassistant` (not only the clone subfolder):

```bash
# After cloning minersteuerung to ~/minersteuerung
ln -sfn ~/minersteuerung/homeassistant ~/homeassistant

# HA Python venv (not in git — recreate)
cd ~/homeassistant
python3 -m venv venv
source venv/bin/activate
pip install 'homeassistant'   # or match .HA_VERSION in config/

# Bridge credentials
mkdir -p ~/.config/ha-bridge
cp ~/homeassistant/ha-bridge/credentials.json ~/.config/ha-bridge/
chmod 600 ~/.config/ha-bridge/credentials.json

# Enable services
cp ~/minersteuerung/miner-dashboard/docs/systemd-user/home-assistant.service \
   ~/minersteuerung/miner-dashboard/docs/systemd-user/ha-data-bridge.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-assistant.service ha-data-bridge.service
```

Excluded from git (rebuild/runtime): `venv/`, `home-assistant_v2.db*`, logs.
