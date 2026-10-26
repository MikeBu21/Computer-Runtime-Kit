from playwright.sync_api import sync_playwright

def login_and_read_miners(playwright=None):
    print("🌐 Starte Miner-Dashboard-Login...")

    if playwright is None:
        playwright = sync_playwright().start()

    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    try:
        page.goto("http://192.168.1.141:5000/login", timeout=60000)

        print("🔐 Fülle Loginformular aus...")
        page.fill("input[name='username']", "miningfarm")
        page.fill("input[name='password']", "Beispiel")
        page.click("button[type='submit']")

        print("⏳ Warte auf Dashboard-Ladeanzeige...")
        page.wait_for_selector("div.miner-box", timeout=15000)

        print("✅ Miner-Dashboard bereit.")

        miners = []
        miner_boxes = page.locator("div.miner-box")

        count = miner_boxes.count()
        for i in range(count):
            box = miner_boxes.nth(i)

            def safe_get_input(name):
                el = box.locator(f'input[name="{name}"]')
                return el.input_value() if el.count() > 0 else ""

            miners.append({
                "ip": box.locator("strong").inner_text(),
                "auto_active": box.locator("input[name='auto']").is_checked(),
                "thresholds": {
                    "pause": int(safe_get_input("pause_kw") or 9999),
                    "resume": int(safe_get_input("resume_kw") or 0),
                    "ths_low": int(safe_get_input("lower_kw") or 9999),
                    "ths_high": int(safe_get_input("raise_kw") or -9999),
                },
                "buttons": {
                    "pause": box.locator("a[href^='/pause']"),
                    "resume": box.locator("a[href^='/resume']"),
                    "low": box.locator("form[action='/set_ths']").nth(1).locator("button"),
                    "high": box.locator("form[action='/set_ths']").nth(0).locator("button"),
                 },
                "last_action": None,
            })

        return browser, page, miners

    except Exception as e:
        print("❌ Fehler im Miner-Dashboard:", e)
        browser.close()
        return None, None, []


