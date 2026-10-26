#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enline cloud.enline.at/power – Kelag + Tabellenzeilen lesen.
Debug: ENLINE_DEBUG=1
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from playwright.sync_api import Page, sync_playwright

LOGIN_URL = "https://www.energie-plan.at/login/"
POWER_URL = "https://cloud.enline.at/power"
PAGE_REFRESH_INTERVAL_SECONDS = 12 * 3600  # 12 Stunden
DEBUG = os.environ.get("ENLINE_DEBUG", "").strip().lower() in ("1", "true", "yes")
DEBUG_DIR = Path(__file__).resolve().parent / "enline_debug"

TABLE_ROW_SELECTOR = '#tariffcircuits tr[class*="tariffcircuit"], #tariffcircuits tbody tr:has(.progressBarText)'

FIND_ROW_COUNT_JS = """
() => {
  const rows = Array.from(document.querySelectorAll('tr')).filter(tr =>
    /tariffcircuit/i.test(String(tr.className || ''))
  );
  if (rows.length) return rows.length;
  const tbody = document.querySelector('#tariffcircuits tbody');
  return tbody ? tbody.querySelectorAll('tr .progressBarText').length : 0;
}
"""

LIST_ROWS_JS = """
() => {
  let rows = Array.from(document.querySelectorAll('tr')).filter(tr =>
    /tariffcircuit/i.test(String(tr.className || ''))
  );
  if (!rows.length) {
    const tbody = document.querySelector('#tariffcircuits tbody');
    if (tbody) {
      rows = Array.from(tbody.querySelectorAll('tr')).filter(tr =>
        tr.querySelector('.progressBarText')
      );
    }
  }
  const result = [];
  for (const row of rows) {
    const nameCell = row.querySelector('td:first-child');
    const valEl = row.querySelector('td.value-col .progressBarText, .progressBarText');
    const unitEl = row.querySelector('td.value-col .progressBarUnit, .progressBarUnit');
    let name = '';
    if (nameCell) {
      const inner = nameCell.querySelector('span, font');
      name = (inner ? inner.innerText : nameCell.innerText) || '';
    }
    result.push({
      name: name.trim(),
      value: valEl ? (valEl.innerText || '').trim() : '',
      unit: unitEl ? (unitEl.innerText || '').trim() : '',
    });
  }
  const loginEl = document.getElementById('login');
  const loginVisible = !!(loginEl && loginEl.querySelector('input[name="username"]')
    && loginEl.offsetParent !== null);
  return {
    rowCount: rows.length,
    hasTariffCircuits: !!document.getElementById('tariffcircuits'),
    onLoginPage: loginVisible,
    title: document.title,
    url: location.href,
    rows: result,
  };
}
"""


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[enline_debug] {msg}")


def _parse_kw(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = str(text).replace("\xa0", " ").strip()
    m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(kW|kw|W|w)?", cleaned, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    if (m.group(2) or "kw").lower() == "w":
        return round(val / 1000.0, 3)
    return val


def _creds(page: Page) -> Tuple[Optional[str], Optional[str]]:
    return getattr(page, "_enline_credentials", (None, None))


def _row_count(target: Page) -> int:
    try:
        return int(target.evaluate(FIND_ROW_COUNT_JS) or 0)
    except Exception:
        return 0


def _has_cloud_login_form(page: Page) -> bool:
    try:
        user = page.locator('#login input[name="username"]')
        pw = page.locator('#login input[name="password"]')
        return user.count() > 0 and pw.count() > 0 and user.first.is_visible()
    except Exception:
        return False


def _save_debug(page: Page, step: str) -> None:
    if not DEBUG:
        return
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    base = DEBUG_DIR / f"{ts}_{step}"
    try:
        page.screenshot(path=str(base) + ".png", full_page=True)
        Path(str(base) + ".html").write_text(page.content(), encoding="utf-8", errors="replace")
        info = _read_table(page)
        Path(str(base) + "_rows.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _log(
            f"{step}: Zeilen={info.get('rowCount')} "
            f"Login={info.get('onLoginPage')} Titel={info.get('title')!r} URL={page.url}"
        )
    except Exception as e:
        _log(f"{step}: debug save failed: {e}")


def _new_browser(headless: bool = True):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="de-AT",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    return playwright, browser, page


def _login_cloud(page: Page, username: str, password: str) -> None:
    if not _has_cloud_login_form(page):
        return
    _log("Cloud-Login auf cloud.enline.at")
    page.locator('#login input[name="username"]').fill(username, timeout=8000)
    page.locator('#login input[name="password"]').fill(password, timeout=8000)
    page.locator("#perform-login").click(timeout=10000)
    _wait_table_rows(page, 25.0)


def _login_energie_plan(page: Page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, timeout=45000, wait_until="domcontentloaded")
    _save_debug(page, "01_login_page")

    form = page.locator("form").filter(has=page.locator('input[type="password"]')).first
    form.wait_for(state="visible", timeout=15000)
    form.locator('input[type="text"]:visible, input[type="email"]:visible').first.fill(
        username, timeout=8000
    )
    form.locator('input[type="password"]:visible').first.fill(password, timeout=8000)
    btn = form.locator('button:visible:has-text("Anmelden")')
    if btn.count() > 0:
        btn.first.click(timeout=10000)
    else:
        form.locator('input[type="password"]').first.press("Enter")

    deadline = time.time() + 15
    while time.time() < deadline:
        if "cloud.enline.at" in page.url:
            break
        for sel in ('a[href*="cloud.enline.at"]', 'a[href*="/power"]'):
            try:
                link = page.locator(sel).first
                if link.count() and link.is_visible():
                    link.click(timeout=4000)
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    break
            except Exception:
                continue
        time.sleep(0.25)


def _login(page: Page, username: str, password: str) -> None:
    page._enline_credentials = (username, password)  # type: ignore[attr-defined]

    page.goto(POWER_URL, timeout=45000, wait_until="domcontentloaded")
    if _has_cloud_login_form(page):
        _login_cloud(page, username, password)
    elif _row_count(page) == 0:
        _login_energie_plan(page, username, password)
        if _has_cloud_login_form(page):
            _login_cloud(page, username, password)

    _go_power(page)
    _save_debug(page, "02_after_login")


def _wait_table_rows(page: Page, timeout_sec: float = 40.0) -> int:
    if _row_count(page) > 0:
        return _row_count(page)
    try:
        page.wait_for_selector(TABLE_ROW_SELECTOR, state="attached", timeout=int(timeout_sec * 1000))
    except Exception:
        pass
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        n = _row_count(page)
        if n > 0:
            _log(f"Tabelle: {n} Zeilen")
            return n
        time.sleep(0.2)
    return _row_count(page)


def _go_power(page: Page) -> int:
    """Zur Leistungsseite – kein erneutes goto wenn Tabelle schon da ist."""
    n = _row_count(page)
    if n > 0 and "/power" in page.url:
        page._enline_power_ready = True  # type: ignore[attr-defined]
        return n

    if "/power" not in page.url:
        try:
            link = page.locator('a[href="/power"], a.active[href*="power"]').first
            if link.count() and link.is_visible():
                link.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            else:
                page.goto(POWER_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception:
            page.goto(POWER_URL, timeout=45000, wait_until="domcontentloaded")
    elif n == 0:
        page.goto(POWER_URL, timeout=45000, wait_until="domcontentloaded")

    username, password = _creds(page)
    if username and password and _has_cloud_login_form(page):
        _login_cloud(page, username, password)

    n = _wait_table_rows(page, 40.0)
    if n > 0:
        page._enline_power_ready = True  # type: ignore[attr-defined]
    return n


def _read_table(page: Page) -> Dict[str, Any]:
    try:
        return page.evaluate(LIST_ROWS_JS)
    except Exception as e:
        return {"error": str(e), "url": page.url, "rowCount": 0, "rows": []}


def _ensure_power_page(page: Page, *, force: bool = False) -> None:
    if not force and getattr(page, "_enline_power_ready", False) and _row_count(page) > 0:
        return
    if not force and _row_count(page) > 0 and "/power" in page.url:
        page._enline_power_ready = True  # type: ignore[attr-defined]
        return

    n = _go_power(page)
    if n == 0:
        _log("WARNUNG: Keine Tabellenzeilen auf /power")
    _save_debug(page, "03_power_page")


def _kelag_from_data(data: Dict[str, Any]) -> Optional[float]:
    for row in data.get("rows", []):
        if "kelag" not in (row.get("name") or "").lower():
            continue
        raw = f"{row.get('value', '')} {row.get('unit', '')}".strip()
        kw = _parse_kw(raw) or _parse_kw(row.get("value", ""))
        if kw is not None:
            return kw
    return None


def _read_kelag(page: Page) -> Optional[float]:
    data = _read_table(page)
    if DEBUG:
        for row in data.get("rows", []):
            if "kelag" in (row.get("name") or "").lower():
                _log(f"Kelag-Zeile: {row}")
    kw = _kelag_from_data(data)
    if kw is not None:
        return kw
    try:
        r = page.locator(TABLE_ROW_SELECTOR).filter(has_text=re.compile(r"kelag", re.I)).first
        if r.count():
            return _parse_kw(r.locator(".progressBarText").first.inner_text(timeout=2000))
    except Exception:
        pass
    return None


def get_latest_kelag_value(page: Page) -> Optional[float]:
    if _row_count(page) == 0:
        _ensure_power_page(page)
    v = _read_kelag(page)
    if v is not None:
        return v
    _log("Kelag leer – Tabelle erneut laden")
    page._enline_power_ready = False  # type: ignore[attr-defined]
    _go_power(page)
    return _read_kelag(page)


def observe_page(page: Page) -> Dict[str, Any]:
    if _row_count(page) == 0:
        _ensure_power_page(page)
    return _read_table(page)


def refresh_power_page(page: Page) -> None:
    """Lädt cloud.enline.at/power neu (gegen DOM-/Speicher-Drift bei Langzeitbetrieb)."""
    page._enline_power_ready = False  # type: ignore[attr-defined]
    _log("Seiten-Refresh cloud.enline.at/power")
    try:
        page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception:
        page.goto(POWER_URL, timeout=45000, wait_until="domcontentloaded")

    username, password = _creds(page)
    if username and password and _has_cloud_login_form(page):
        _login_cloud(page, username, password)

    n = _wait_table_rows(page, 40.0)
    if n > 0:
        page._enline_power_ready = True  # type: ignore[attr-defined]
    _save_debug(page, "04_page_refresh")


def start_session(
    username: str,
    password: str,
    headless: bool = True,
) -> Tuple[Any, Any, Page]:
    playwright, browser, page = _new_browser(headless=headless)
    _login(page, username, password)
    return playwright, browser, page


def kelag_from_data(data: Dict[str, Any]) -> Optional[float]:
    return _kelag_from_data(data)


def should_block_enline_resource(resource_type: str, url: str) -> bool:
    """Playwright-Route: Bilder/Fonts/Media und Tracker blocken, App-Traffic durchlassen."""
    kind = str(resource_type or "").lower()
    href = str(url or "").lower()
    if kind in ("image", "font", "media"):
        return True
    trackers = (
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "facebook.com/tr",
        "hotjar.com",
    )
    return any(t in href for t in trackers)
