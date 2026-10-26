# -*- coding: utf-8 -*-
"""Telegram-Ertragsbefehle und Stromprofil-Bild."""
from telegram_energy import (
    classify_energy_command,
    format_chart_caption,
    format_today_message,
    format_tomorrow_message,
    render_stromprofil_png,
)


def test_classify_buttons_and_commands():
    assert classify_energy_command("Ertrag heute") == "today"
    assert classify_energy_command("Ertrag heute erwartet") == "today"
    assert classify_energy_command("/ertrag") == "today"
    assert classify_energy_command("/ertragheute@MinerBot") == "today"
    assert classify_energy_command("Ertrag morgen") == "tomorrow"
    assert classify_energy_command("/morgen") == "tomorrow"
    assert classify_energy_command("Stromprofil") == "chart"
    assert classify_energy_command("/statistik") == "chart"
    assert classify_energy_command("Energiestatistik") == "chart"
    assert classify_energy_command("/status") is None


def test_today_message_includes_expected_produced_and_weather():
    text = format_today_message(67.3, 67.08, 57.41)
    assert "Erwartet: 67,3 kWh" in text
    assert "Bisher produziert: 67,1 kWh" in text
    assert "Wetterprognose: 57,4 kWh" in text


def test_today_message_hides_duplicate_weather():
    text = format_today_message(40.0, 12.2, 40.4)
    assert "Wetterprognose" not in text
    assert "Bisher produziert: 12,2 kWh" in text


def test_today_message_notes_when_production_exceeds_forecast():
    text = format_today_message(50.0, 67.0, 50.0)
    assert "über der Prognose" in text


def test_tomorrow_message():
    text = format_tomorrow_message(55.87)
    assert "Ertrag morgen" in text
    assert "Erwartet: 55,9 kWh" in text
    assert "produziert" not in text


def test_missing_forecast_is_explicit():
    assert "nicht verfügbar" in format_today_message(None, None, None)
    assert "nicht verfügbar" in format_tomorrow_message(None)


def test_stromprofil_png_from_day_curve():
    samples = []
    for minute in range(0, 18 * 60, 10):
        hour = minute / 60
        sun = max(0.0, 12.0 * (1 - ((hour - 13) / 5) ** 2))
        samples.append(
            {
                "m": minute,
                "line_erzeugung": round(sun, 2),
                "line_netz": round(-sun * 0.45, 2),
                "line_verbrauch": 1.8,
                "battery_kw": round((4 - sun) * 0.15, 2),
                "line_soc": min(100.0, 55 + hour * 2),
            }
        )
    png, last = render_stromprofil_png(samples, "2026-10-18", produced_kwh=42.5)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5000
    assert last["line_erzeugung"] is not None
    assert last["line_soc"] is not None
    caption = format_chart_caption("2026-10-18", last, 42.5)
    assert "18.10.2026" in caption
    assert "Bisher produziert: 42,5 kWh" in caption
    assert "SOC:" in caption


def test_stromprofil_rejects_empty_samples():
    try:
        render_stromprofil_png([], "2026-10-18")
    except ValueError as exc:
        assert "Keine Messwerte" in str(exc)
    else:
        raise AssertionError("leere Messreihe muss fehlschlagen")
