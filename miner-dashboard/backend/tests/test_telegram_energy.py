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
    assert classify_energy_command("Mehrbedarf morgen") == "mehrbedarf"
    assert classify_energy_command("/mehrbedarf") == "mehrbedarf"
    assert classify_energy_command("/status") is None


def test_mehrbedarf_info_is_only_a_question():
    from telegram_energy import format_mehrbedarf_info_message

    text = format_mehrbedarf_info_message()
    assert "Kilowatt" in text
    assert "morgen" in text
    assert "tagsüber" in text


def test_mehrbedarf_kw_roundtrip(tmp_path):
    from telegram_energy import (
        format_mehrbedarf_prompt_message,
        format_mehrbedarf_saved_message,
        load_mehrbedarf_kw,
        parse_mehrbedarf_kw,
        save_mehrbedarf_kw,
    )

    assert parse_mehrbedarf_kw("10") == 10.0
    assert parse_mehrbedarf_kw("2,5 kW") == 2.5
    assert parse_mehrbedarf_kw("-1") is None
    assert parse_mehrbedarf_kw("abc") is None
    saved = save_mehrbedarf_kw(str(tmp_path), "7,5")
    assert saved == 7.5
    assert load_mehrbedarf_kw(str(tmp_path)) == 7.5
    prompt = format_mehrbedarf_prompt_message(7.5)
    assert "Zahl in kW" in prompt
    assert "7,5 kW" in prompt
    saved_text = format_mehrbedarf_saved_message(7.5)
    assert "Danke für die Übernahme" in saved_text
    assert "7,5 kW" in saved_text
    assert "Speicher morgen" in saved_text
    assert parse_mehrbedarf_kw("ich brauche morgen 300 kW mehr") == 300.0
    assert parse_mehrbedarf_kw("ich brauche morgen aber nur 20 kW mehr") == 20.0


def test_latest_mehrbedarf_replaces_the_older_value(tmp_path):
    from telegram_energy import load_mehrbedarf_kw, record_latest_mehrbedarf

    first = record_latest_mehrbedarf(str(tmp_path), "ich brauche morgen 300 kW mehr")
    assert first == 300.0
    assert load_mehrbedarf_kw(str(tmp_path)) == 300.0
    second = record_latest_mehrbedarf(
        str(tmp_path), "ich brauche morgen aber nur 20 kW mehr"
    )
    assert second == 20.0
    assert load_mehrbedarf_kw(str(tmp_path)) == 20.0


def test_mehrbedarf_saved_confirms_300_kw():
    from telegram_energy import format_mehrbedarf_saved_message, parse_mehrbedarf_kw

    value = parse_mehrbedarf_kw("300")
    assert value == 300.0
    text = format_mehrbedarf_saved_message(value)
    assert text.startswith("Danke für die Übernahme.")
    assert "300 kW" in text
    assert "gefüllt bzw. bereitsteht" in text


def test_mehrbedarf_with_future_expires_loads_value(tmp_path):
    from datetime import datetime

    from telegram_energy import load_mehrbedarf_kw, load_mehrbedarf_record, save_mehrbedarf_kw

    now = datetime(2026, 11, 21, 10, 0, 0)
    expires = datetime(2026, 11, 21, 22, 0, 0)
    save_mehrbedarf_kw(str(tmp_path), 12.5, expires_at=expires, now=now)
    assert load_mehrbedarf_kw(str(tmp_path), now=now) == 12.5
    record = load_mehrbedarf_record(str(tmp_path), now=now)
    assert record["kw"] == 12.5
    assert record["expires_at"] == expires


def test_mehrbedarf_with_past_expires_returns_zero(tmp_path):
    from datetime import datetime

    from telegram_energy import load_mehrbedarf_kw, load_mehrbedarf_record, save_mehrbedarf_kw

    now = datetime(2026, 11, 21, 10, 0, 0)
    expires = datetime(2026, 11, 21, 9, 0, 0)
    save_mehrbedarf_kw(str(tmp_path), 12.5, expires_at=expires, now=now)
    assert load_mehrbedarf_kw(str(tmp_path), now=now) == 0.0
    record = load_mehrbedarf_record(str(tmp_path), now=now)
    assert record["kw"] == 0.0
    assert record["expires_at"] == expires


def test_parse_mehrbedarf_expires_bis_clock_and_iso():
    from datetime import datetime

    from telegram_energy import parse_mehrbedarf_expires, parse_mehrbedarf_kw_and_expires

    now = datetime(2026, 11, 21, 10, 0, 0)
    assert parse_mehrbedarf_expires("bis 22:00", now=now) == datetime(2026, 11, 21, 22, 0, 0)
    assert parse_mehrbedarf_expires("22:00", now=now) == datetime(2026, 11, 21, 22, 0, 0)
    assert parse_mehrbedarf_expires("ohne", now=now) is None
    assert parse_mehrbedarf_expires("2026-11-22T18:30", now=now) == datetime(
        2026, 11, 22, 18, 30, 0
    )
    kw, expires = parse_mehrbedarf_kw_and_expires("10 bis 22:00", now=now)
    assert kw == 10.0
    assert expires == datetime(2026, 11, 21, 22, 0, 0)


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
