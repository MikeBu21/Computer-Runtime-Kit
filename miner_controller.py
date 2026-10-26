import time
import requests
import threading
import os
from statistics import mean

# -------------------------
# Konfiguration
# -------------------------

miners = [
    {"ip": "192.168.1.128", "password": "Beispiel"},
    # Hier kannst du bis zu 15 Miner eintragen
    # {"ip": "192.168.1.129", "password": "dein_passwort2"},
]

# Verbrauchswert-Liste für 8 Minuten Durchschnitt (5 Sek Schritte)
consumption_values = []

# URL Endpunkte für Miner Befehle
COMMANDS = {
    "pause": "/brains.boser.web.ActionsService/PauseMining",
    "resume": "/brains.boser.web.ActionsService/ResumeMining"
}

# Schwellenwerte
LIMIT_FULL_STOP = 50  # > 50kW = Alle pausieren
LIMIT_STEP_DOWN = 40  # > 40kW = Schrittweise pausieren
LIMIT_FULL_RESUME = 30  # < 30kW = Alle Miner wieder an
STEP_MINERS = 2  # Immer 2 Miner gleichzeitig pausieren oder resumieren

# Zähler-Dummy (wird durch echten Lesekopf ersetzt)
def read_energy_consumption():
    # Simuliere Verbrauch (später mit echtem SML Daten ersetzen)
    return 38 + (2 * (time.time() % 60) / 60)  # Verbrauch schwankt zwischen 38-40kW

# Miner Steuerung

def control_miner(miner, action):
    try:
        url = f"http://{miner['ip']}{COMMANDS[action]}"
        response = requests.post(
            url,
            auth=requests.auth.HTTPDigestAuth("root", miner["password"]),
            headers={"Content-Type": "application/grpc-web+proto"},
            timeout=5
        )
        if response.status_code == 200:
            print(f"[{miner['ip']}] {action.upper()} erfolgreich!")
            return True
        else:
            print(f"[{miner['ip']}] Fehler beim {action.upper()}: {response.status_code}")
            return False
    except Exception as e:
        print(f"[{miner['ip']}] Ausnahme beim {action.upper()}: {e}")
        return False

# Sicherheits-Neustart Pi

def reboot_pi():
    print("[SYSTEM] Reboot des Raspberry Pi wird ausgeführt...")
    time.sleep(10)
    os.system("sudo reboot")

# Hauptautomatik

def miner_automation():
    global consumption_values
    active_miners = list(miners)  # Alle Miner anfangs aktiv

    print("[SYSTEM] Automatik startet - erste Steuerung erst nach 8 Minuten...")
    first_cycle = True

    while True:
        try:
            # Verbrauch abfragen
            consumption = read_energy_consumption()
            print(f"[ENERGY] Aktueller Verbrauch: {consumption:.2f} kW")

            # Werte speichern für 8 Minuten Durchschnitt
            consumption_values.append(consumption)
            if len(consumption_values) > (8 * 60 // 5):
                consumption_values.pop(0)

            # Nur steuern wenn 8 Minuten vergangen
            if not first_cycle and (len(consumption_values) >= (8 * 60 // 5)):
                avg_consumption = mean(consumption_values)
                print(f"[ENERGY] 8-Minuten Durchschnitt: {avg_consumption:.2f} kW")

                # Steuerlogik
                if avg_consumption > LIMIT_FULL_STOP:
                    print("[ACTION] >50kW erkannt, alle Miner pausieren...")
                    for miner in miners:
                        control_miner(miner, "pause")
                    active_miners = []

                elif avg_consumption > LIMIT_STEP_DOWN:
                    print("[ACTION] >40kW erkannt, schrittweises Pausieren...")
                    if len(active_miners) >= STEP_MINERS:
                        for _ in range(STEP_MINERS):
                            miner = active_miners.pop()
                            control_miner(miner, "pause")

                elif avg_consumption < LIMIT_FULL_RESUME:
                    print("[ACTION] <30kW erkannt, alle Miner resume...")
                    for miner in miners:
                        control_miner(miner, "resume")
                    active_miners = list(miners)

                elif avg_consumption < LIMIT_STEP_DOWN:
                    print("[ACTION] <40kW erkannt, schrittweises Resumieren...")
                    paused_miners = [m for m in miners if m not in active_miners]
                    if len(paused_miners) >= STEP_MINERS:
                        for _ in range(STEP_MINERS):
                            miner = paused_miners.pop()
                            control_miner(miner, "resume")
                            active_miners.append(miner)

            else:
                if first_cycle:
                    print("[INFO] Erste 8 Minuten Sammelphase, noch keine Steuerung...")

            first_cycle = False
            time.sleep(5)

        except Exception as e:
            print(f"[SYSTEM] Schwerer Fehler: {e}")
            reboot_pi()

# Starten
if __name__ == "__main__":
    miner_automation()

