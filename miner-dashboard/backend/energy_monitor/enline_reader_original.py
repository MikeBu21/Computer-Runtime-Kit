#!/usr/bin/env python3
"""
Original-Aufbau: nur Login + Kelag-Wert von cloud.enline.at/power
(API wie früher: start_session(user, pw) und get_latest_kelag_value(page))
"""
import re
import time
from typing import Any, Optional, Tuple

from playwright.sync_api import Page, sync_playwright

LOGIN_URL = "https://www.energie-plan.at/login/"
POWER_URL = "https://cloud.enline.at/power"


def _parse_kw(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(kW|kw|W|w)?", text.replace("\xa0", " "), re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    if (m.group(2) or "kw").lower() == "w":
        return round(val / 1000.0, 3)
    return val


def _login(page: Page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, timeout=90000, wait_until="domcontentloaded")
    form = page.locator("form").filter(has=page.locator('input[type="password"]')).first
    form.wait_for(state="visible", timeout=30000)
    form.locator(
        'input[type="text"]:visible, input[type="email"]:visible'
    ).first.fill(username, timeout=15000)
    form.locator('input[type="password"]:visible').first.fill(password, timeout=15000)
    btn = form.locator('button:visible:has-text("Anmelden")')
    if btn.count() > 0:
        btn.first.click(timeout=20000)
    else:
        form.locator('input[type="password"]').first.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=60000)


def _open_power_page(page: Page) -> None:
    page.goto(POWER_URL, timeout=90000, wait_until="domcontentloaded")
    for _ in range(20):
        if page.locator("#tariffCircuits tr.tariffCircuitItem").count() > 0:
            return
        if page.locator("tr.tariffCircuitItem").count() > 0:
            return
        time.sleep(0.5)
    time.sleep(2)


def get_latest_kelag_value(page: Page) -> Optional[float]:
    """99 Kelag (UMG) Kontrollmessung aus der Leistungstabelle."""
    _open_power_page(page)

    for row_sel in (
        "#tariffCircuits tr.tariffCircuitItem",
        "table.datatable tr.tariffCircuitItem",
        "tr.tariffCircuitItem",
    ):
        rows = page.locator(row_sel)
        n = rows.count()
        if n == 0:
            continue
        for i in range(n):
            row = rows.nth(i)
            try:
                if row.locator("td.name_col").count():
                    name = row.locator("td.name_col").first.inner_text(timeout=2000)
                else:
                    name = row.locator("td").first.inner_text(timeout=2000)
            except Exception:
                continue
            if "kelag" not in name.lower():
                continue
            try:
                val_col = row.locator("td.value_col .progressBarText").first
                raw = val_col.inner_text(timeout=2000).strip()
                unit = ""
                try:
                    unit = row.locator("td.value_col .progressBarUnit").first.inner_text(timeout=1000)
                except Exception:
                    pass
                kw = _parse_kw(f"{raw} {unit}".strip())
                if kw is not None:
                    return kw
            except Exception:
                kw = _parse_kw(row.inner_text(timeout=2000))
                if kw is not None:
                    return kw
        break
    return None


def start_session(
    username: str,
    password: str,
    headless: bool = True,
) -> Tuple[Any, Any, Page]:
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page()
    _login(page, username, password)
    _open_power_page(page)
    return playwright, browser, page
