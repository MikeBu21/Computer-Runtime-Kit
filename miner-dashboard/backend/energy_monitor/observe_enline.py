#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enline-Lesevorgang beobachten (Debug).
  ENLINE_DEBUG=1 python observe_enline.py
"""
import json
import os
import sys
import time

from enline_reader import DEBUG_DIR, _parse_kw, kelag_from_data, observe_page, start_session

USER = "ab@buttazoni.at"
PASS = "zMYBWipTVzQul7TEOktW"


def main():
    os.environ["ENLINE_DEBUG"] = "1"
    print("=== Enline Beobachtung ===")
    print("Debug-Ordner:", DEBUG_DIR)
    print()

    t0 = time.time()
    pw, browser, page = start_session(USER, PASS, headless=True)
    try:
        print("URL:", page.url)
        data = observe_page(page)
        elapsed = round(time.time() - t0, 1)
        print(f"\nDauer: {elapsed}s")
        print("\n--- Tabellen-Inhalt ---")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("\nrowCount:", data.get("rowCount", 0))

        kelag = kelag_from_data(data)
        print("\nKelag:", kelag)
        for row in data.get("rows", []):
            if "wohnhaus" in (row.get("name") or "").lower():
                raw = f"{row.get('value', '')} {row.get('unit', '')}".strip()
                print("Wohnhaus:", raw, "->", _parse_kw(raw), "kW")
                break

        if data.get("rowCount", 0) == 0:
            print("\n!!! rowCount=0 -> enline_debug/*_03_power_page.html prüfen")
            return 1
        if kelag is None:
            print("\n!!! Kelag-Zeile nicht gefunden (Tabelle da?)")
            return 1
        print("\nOK – Lesen funktioniert.")
        return 0
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
