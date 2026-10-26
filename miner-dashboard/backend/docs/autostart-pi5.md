# Autostart nach Pi-Neustart

## Installation (einmalig)

```bash
chmod +x /home/piMiner/miner-dashboard/start_all.sh
sudo cp /home/piMiner/miner-dashboard/miner-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable miner-dashboard.service
sudo systemctl start miner-dashboard.service
```

Status prüfen:

```bash
sudo systemctl status miner-dashboard.service
ls -la /home/piMiner/miner-dashboard/logs/
```

## Hinweise

- Ohne grafische Oberfläche (SSH/Headless) startet `start_all.sh` alle Prozesse im Hintergrund; Logs liegen unter `logs/`.
- Mit Desktop werden weiterhin `lxterminal`-Fenster geöffnet.
- Der **Steuerungsalgorithmus** läuft als `control_algorithm_daemon.py` mit.

## Manuell starten

```bash
/home/piMiner/miner-dashboard/start_all.sh
```
