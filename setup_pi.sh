# Setup-Skript für Raspberry Pi 5
# Speichern als: setup_pi.sh

#!/bin/bash

echo "### Update & Upgrade starten..."
sudo apt-get update && sudo apt-get upgrade -y

echo "### Python3 & Pip installieren..."
sudo apt-get install python3 python3-pip -y

echo "### Serielle Kommunikationstools installieren..."
sudo apt-get install python3-serial screen -y

echo "### Git installieren (falls nicht vorhanden)..."
sudo apt-get install git -y

echo "### Auto-Reboot Tools installieren..."
sudo apt-get install watchdog -y
sudo systemctl enable watchdog
sudo systemctl start watchdog

echo "### System bereit für das Projekt!"

echo "### USB Geräte anzeigen..."
lsusb

echo "### Setup abgeschlossen! Du kannst jetzt die Projektdateien hochladen."


