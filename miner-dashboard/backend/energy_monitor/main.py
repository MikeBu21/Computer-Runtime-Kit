from playwright.sync_api import sync_playwright
import time
from statistics import mean
import traceback

# 🔐 Zugangsdaten
USERNAME = "ab@buttazoni.at"
PASSWORD = "zMYBWipTVzQul7TEOktW"

# Konfiguration
INTERVAL_SECONDS = 6
ROLLING_WINDOW = 450  # 7.5 Minuten
RELOGIN_INTERVAL = 7200  # 2 Stunden in Sekunden

def start_session(username, password):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    print("🔐 Logging in...")
    page.goto("https://cloud.enline.at/power", timeout=60000)

    page.wait_for_selector('input[name="username"]', timeout=30000)
    page.fill('input[name="username"]', username)

    page.wait_for_selector('input[name="password"]', timeout=30000)
    page.fill('input[name="password"]', password)

    page.wait_for_selector('button#perform-login', timeout=30000)
    page.click('button#perform-login')

    print("⏳ Warte auf Dashboard...")
    page.wait_for_timeout(8000)

    return playwright, browser, page

def read_kelag_value(page):
    try:
        kelag_text_element = page.locator("text=99 Kelag (UMG) Kontrollmessung").first

        kelag_value_element = kelag_text_element.locator(
            "xpath=ancestor::tr/td[contains(@class, 'value-col')]//div[@class='progressBarText']"
        ).first

        value_str = kelag_value_element.inner_text(timeout=2000)
        value = float(value_str.replace(",", ".").strip())
        print(f"📈 Aktueller Wert: {value} kW")
        return value
    except Exception as e:
        print("⚠️ Fehler beim Auslesen:", e)
        return None

def run_loop():
    values = []
    last_reset = time.time()

    playwright, browser, page = None, None, None

    try:
        playwright, browser, page = start_session(USERNAME, PASSWORD)

        print("🔁 Starte Überwachung... [STRG+C zum Beenden]")

        while True:
            now = time.time()

            # 🔁 Re-login nach 2 Stunden
            if now - last_reset >= RELOGIN_INTERVAL:
                print("♻️  Relogin nach 2 Stunden...")
                browser.close()
                playwright.stop()
                playwright, browser, page = start_session(USERNAME, PASSWORD)
                last_reset = now
                values.clear()

            value = read_kelag_value(page)
            if value is not None:
                values.append(value)
                # Nur die letzten 450 / 6 = 75 Werte behalten (7,5 Minuten)
                if len(values) > 75:
                    values.pop(0)

            # ⏱ Durchschnitt ausgeben
            if len(values) >= 10:
                avg = round(mean(values), 2)
                print(f"📊 Durchschnitt (letzte 7,5 min): {avg} kW")

            time.sleep(INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("🛑 Manuell beendet.")
    except Exception as e:
        print("❌ Unerwarteter Fehler:", e)
        traceback.print_exc()
        print("🔄 Versuche Neustart in 10 Sekunden...")
        time.sleep(10)
        run_loop()  # Neustart bei Fehler
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()

# ▶️ Start
if __name__ == "__main__":
    print("🚀 Energieüberwachung startet...")
    run_loop()

