# Update-Anleitung

So holst du die neueste Version von `MikeBu21/Computer-Runtime-Kit` auf ein Gerät,
auf dem die Software bereits läuft.

## Kurz

1. Dashboard öffnen.
2. Bereich **Öffentliches Update**.
3. **Update herunterladen** tippen.
4. Warten, bis der Fortschrittsbalken bei 100 % ist.

## Was wird aktualisiert?

Nur Programmcode (zum Beispiel `.py`, `.html`, `.css`, `.js`, `.sh`).

Zusätzlich richtet der Download die Auto-Start-Struktur ein (Desktop-Eintrag,
Startreihenfolge, Statusfenster) und startet die Software-Dienste neu
(Scheduler, Batterie-Scraper, Flask-Backend, Energiemonitor,
Steuerungsalgorithmus, Telegram-Bot). Der Computer wird **nicht** neu gebootet.

## Was bleibt erhalten?

- Persönliche Einstellungen
- Geräte und Zuordnungen
- Passwörter und Tokens
- Chat-IDs
- Lernstände / Neuronen
- Historie und Trainingsdaten

## Erster Start ohne vorhandene Installation

```bash
git clone https://github.com/MikeBu21/Computer-Runtime-Kit.git ~/minersteuerung
```

Danach lokal einrichten. Der Button **Update herunterladen** ist in dieser
Version bereits enthalten und holt künftige Updates aus demselben Repo.
