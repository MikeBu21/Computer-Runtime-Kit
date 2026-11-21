# -*- coding: utf-8 -*-
"""Telegram-Befehle für PV-Ertrag und Stromprofil."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Dict[str, Any]

_SERIES = (
    {"key": "battery_kw", "label": "Batterie", "color": (75, 85, 99), "axis": "kw", "width": 3},
    {"key": "line_verbrauch", "label": "Verbrauch", "color": (245, 176, 40), "axis": "kw", "width": 4},
    {"key": "line_netz", "label": "Netz", "color": (126, 87, 194), "axis": "kw", "width": 4},
    {"key": "line_erzeugung", "label": "Produktion", "color": (46, 160, 67), "axis": "kw", "width": 5},
    {"key": "line_soc", "label": "SOC", "color": (30, 136, 229), "axis": "pct", "width": 5},
)

_FONT_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)
_FONT_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


# Hinweis plus optionale kW-Eingabe. Speichert nur den Merker, schreibt keine Register.
MEHRBEDARF_INFO_TEXT = (
    "Morgen scheint die Sonne, und genau dann zählt dein Mehrbedarf. "
    "Wie viele Kilowatt brauchst du morgen tagsüber mehr als sonst?"
)
MEHRBEDARF_PROMPT_TEXT = (
    MEHRBEDARF_INFO_TEXT
    + "\n\nBitte antworte mit einer Zahl in kW, zum Beispiel 10 oder 2,5. "
    "Optional mit Ablauf, zum Beispiel 10 bis 22:00. Zum Abbrechen /cancel."
)
MEHRBEDARF_EXPIRES_PROMPT_TEXT = (
    "Bis wann gilt der Mehrbedarf?\n"
    "Zum Beispiel 22:00, bis 22:00 oder 2026-11-22 22:00.\n"
    "Ohne Ablaufzeit antworte mit ohne oder -. Zum Abbrechen /cancel."
)
MEHRBEDARF_FILE = "mehrbedarf_morgen.json"
_MEHRBEDARF_NO_EXPIRES = frozenset(
    {"", "-", "ohne", "kein", "keine", "nein", "none", "null", "n/a"}
)


def format_mehrbedarf_info_message() -> str:
    """Frage für den Info-Button Mehrbedarf morgen."""
    return MEHRBEDARF_INFO_TEXT


def format_mehrbedarf_prompt_message(current_kw: Optional[float] = None) -> str:
    """Frage plus Eingabehinweis. Optional mit bisher gespeichertem Wert."""
    text = MEHRBEDARF_PROMPT_TEXT
    if current_kw is not None:
        text += f"\nBisher gespeichert: {format_kw(current_kw)}."
    return text


def format_mehrbedarf_expires_prompt_message() -> str:
    """Zweite Dialogfrage: Ablaufzeit für den gespeicherten Mehrbedarf."""
    return MEHRBEDARF_EXPIRES_PROMPT_TEXT


def format_kw(value: Optional[float]) -> str:
    if value is None:
        return "nicht gesetzt"
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " kW"


def format_mehrbedarf_expires(expires_at: Optional[datetime]) -> str:
    if expires_at is None:
        return "ohne Ablauf"
    return expires_at.strftime("%d.%m.%Y %H:%M")


def format_mehrbedarf_saved_message(
    kw: float,
    expires_at: Optional[datetime] = None,
) -> str:
    """Kurze Bestätigung, nachdem eine kW-Zahl übernommen wurde."""
    expiry = ""
    if expires_at is not None:
        expiry = f" Gültig bis {format_mehrbedarf_expires(expires_at)}."
    return (
        f"Danke für die Übernahme. {format_kw(kw)} sind notiert.{expiry} "
        "Wir werden uns drum kümmern, dass der Speicher morgen gefüllt bzw. bereitsteht."
    )


def _mehrbedarf_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / MEHRBEDARF_FILE


def mehrbedarf_expires_iso(expires_at: Optional[datetime]) -> Optional[str]:
    if expires_at is None:
        return None
    return expires_at.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def load_mehrbedarf_record(
    base_dir: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Gespeicherter Mehrbedarf. Nach Ablaufzeit gilt kw als 0."""
    empty: Dict[str, Any] = {"kw": None, "override_kw": None, "expires_at": None}
    path = _mehrbedarf_path(base_dir)
    if not path.is_file():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    expires_at = parse_mehrbedarf_expires(raw.get("expires_at"), now=now)
    clock = now or datetime.now()
    if expires_at is not None and clock > expires_at:
        return {"kw": 0.0, "override_kw": None, "expires_at": expires_at}
    return {
        "kw": parse_mehrbedarf_kw(raw.get("kw")),
        "override_kw": parse_mehrbedarf_kw(raw.get("override_kw")),
        "expires_at": expires_at,
    }


def load_mehrbedarf_kw(
    base_dir: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    return load_mehrbedarf_record(base_dir, now=now).get("kw")


def save_mehrbedarf_kw(
    base_dir: str,
    kw: float,
    override_kw: Any = None,
    *,
    set_override: bool = False,
    expires_at: Any = None,
    now: Optional[datetime] = None,
) -> float:
    value = parse_mehrbedarf_kw(kw)
    if value is None:
        raise ValueError("Ungültiger Mehrbedarf.")
    # Raw-Datei lesen, damit ein abgelaufener Override nicht still verworfen wird.
    path = _mehrbedarf_path(base_dir)
    current_override: Optional[float] = None
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            current_override = parse_mehrbedarf_kw(raw.get("override_kw"))
    override = current_override
    if set_override:
        if override_kw in (None, "", "nein", "none"):
            override = None
        else:
            override = parse_mehrbedarf_kw(override_kw)
            if override is None:
                raise ValueError("Ungültige Übersteuerung.")
    expires = parse_mehrbedarf_expires(expires_at, now=now)
    if expires_at not in (None, "") and str(expires_at).strip().lower() not in _MEHRBEDARF_NO_EXPIRES:
        if expires is None:
            raise ValueError("Ungültige Ablaufzeit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kw": value,
        "override_kw": override,
        "expires_at": mehrbedarf_expires_iso(expires),
        "updated_at": date.today().isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return value


def _mehrbedarf_sentence(text: str) -> bool:
    return any(token in text for token in ("kw", "kilowatt", "mehrbedarf", "brauche"))


def parse_mehrbedarf_kw(raw: Any) -> Optional[float]:
    """kW aus einer Zahl oder einem Satz. Die letzte Zahl im Satz gilt.

    Komma oder Punkt, nicht negativ, höchstens 1000. Ein neuer Aufruf ersetzt
    den vorherigen Wert, es bleibt kein älterer Mehrbedarf stehen.
    Ablaufteile wie „bis 22:00“ werden vor dem Parsen entfernt.
    """
    if raw is None or raw is False:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    text = re.sub(
        r"\bbis\s+\d{1,2}[:.]\d{2}(?:\s*(?:uhr)?)?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}(?::\d{2})?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    compact = text.replace("kilowatt", "").replace("kw", "").replace(" ", "").replace(",", ".")
    try:
        value = float(compact)
    except (TypeError, ValueError):
        if not _mehrbedarf_sentence(text):
            return None
        numbers = re.findall(r"\d+(?:[.,]\d+)?", text)
        if not numbers:
            return None
        try:
            value = float(numbers[-1].replace(",", "."))
        except (TypeError, ValueError):
            return None
    if value < 0 or value > 1000:
        return None
    return round(value, 2)


def parse_mehrbedarf_expires(
    raw: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Ablaufzeit aus ISO, datetime-local, „22:00“ oder „bis 22:00“.

    Nur Uhrzeit: heute zu dieser Stunde, oder morgen wenn die Uhrzeit schon vorbei ist.
    „ohne“ / „-“ / leer → kein Ablauf.
    """
    if raw is None or raw is False:
        return None
    if isinstance(raw, datetime):
        return raw.replace(second=0, microsecond=0)
    text = str(raw).strip()
    if not text or text.lower() in _MEHRBEDARF_NO_EXPIRES:
        return None
    clock = now or datetime.now()
    cleaned = re.sub(r"(?i)^\s*bis\s+", "", text).strip()
    cleaned = cleaned.replace(",", " ")
    cleaned = re.sub(r"(?i)\s*uhr\s*$", "", cleaned).strip()
    cleaned = cleaned.replace("T", " ")
    # 22:00 / 9:30
    time_only = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::(\d{2}))?", cleaned)
    if time_only:
        hour = int(time_only.group(1))
        minute = int(time_only.group(2))
        if hour > 23 or minute > 59:
            return None
        candidate = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= clock:
            candidate += timedelta(days=1)
        return candidate
    normalized = cleaned
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(normalized, fmt).replace(second=0, microsecond=0)
        except ValueError:
            continue
    return None


def parse_mehrbedarf_kw_and_expires(
    raw: Any,
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[float], Optional[datetime]]:
    """kW und optionale Ablaufzeit aus einer Antwort (z. B. „10 bis 22:00“)."""
    text = "" if raw is None else str(raw).strip()
    expires = None
    if text:
        bis_match = re.search(
            r"(?i)\bbis\s+(\d{1,2}[:.]\d{2}(?:\s*(?:uhr)?)?|\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}[:.]\d{2})",
            text,
        )
        if bis_match:
            expires = parse_mehrbedarf_expires(bis_match.group(0), now=now)
        else:
            # Trailing datetime / time after the kW number
            trail = re.search(
                r"(?i)(?:^|\s)(\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}[:.]\d{2}|\d{1,2}[:.]\d{2}(?:\s*uhr)?)\s*$",
                text,
            )
            if trail and not re.fullmatch(
                r"\d+(?:[.,]\d+)?(?:\s*k(?:ilo)?w(?:att)?)?",
                text,
                flags=re.IGNORECASE,
            ):
                expires = parse_mehrbedarf_expires(trail.group(1), now=now)
    return parse_mehrbedarf_kw(raw), expires


def record_latest_mehrbedarf(base_dir: str, text: Any) -> Optional[float]:
    """Übernimmt nur die jüngste kW-Angabe und verwirft die davor gespeicherte."""
    value, expires = parse_mehrbedarf_kw_and_expires(text)
    if value is None:
        return None
    return save_mehrbedarf_kw(base_dir, value, expires_at=expires)


def classify_energy_command(text: str) -> Optional[str]:
    """today, tomorrow, chart oder mehrbedarf. Unbekannter Text ergibt None."""
    raw = (text or "").strip().lower()
    if raw.startswith("/"):
        raw = raw[1:]
        raw = raw.split("@", 1)[0]
        raw = raw.split()[0]
    aliases = {
        "ertrag heute": "today",
        "ertrag": "today",
        "ertrag heute erwartet": "today",
        "ertrag_heute": "today",
        "ertragheute": "today",
        "ertrag morgen": "tomorrow",
        "morgen": "tomorrow",
        "ertrag morgen erwartet": "tomorrow",
        "ertrag_morgen": "tomorrow",
        "ertragmorgen": "tomorrow",
        "stromprofil": "chart",
        "statistik": "chart",
        "energiestatistik": "chart",
        "energie statistik": "chart",
        "mehrbedarf morgen": "mehrbedarf",
        "mehrbedarf": "mehrbedarf",
        "info mehrbedarf": "mehrbedarf",
    }
    return aliases.get(raw)


def format_kwh(value: Optional[float]) -> str:
    if value is None:
        return "nicht verfügbar"
    return f"{value:.1f}".replace(".", ",") + " kWh"


def format_today_message(
    expected: Optional[float],
    produced: Optional[float],
    weather: Optional[float],
) -> str:
    lines = ["☀️ Ertrag heute", ""]
    lines.append(f"Erwartet: {format_kwh(expected)}")
    lines.append(f"Bisher produziert: {format_kwh(produced)}")
    if (
        expected is not None
        and weather is not None
        and abs(float(expected) - float(weather)) >= 1.0
    ):
        lines.append(f"Wetterprognose: {format_kwh(weather)}")
    if (
        expected is not None
        and produced is not None
        and float(produced) > float(expected) + 1.0
    ):
        lines.append("Die Produktion liegt bisher über der Prognose.")
    return "\n".join(lines)


def format_tomorrow_message(expected: Optional[float]) -> str:
    return "\n".join(
        [
            "🌤️ Ertrag morgen",
            "",
            f"Erwartet: {format_kwh(expected)}",
        ]
    )


def _as_float(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def load_energy_snapshot(base_dir: str) -> Dict[str, Optional[float]]:
    """Erwarteter und bisheriger PV-Ertrag aus Prognose und Tagesstatistik."""
    result: Dict[str, Optional[float]] = {
        "expected_today_kwh": None,
        "produced_today_kwh": None,
        "weather_today_kwh": None,
        "expected_tomorrow_kwh": None,
    }
    try:
        from control_algorithm.config import load_state

        state = load_state()
    except Exception as exc:
        print(f"Ertragsprognose nicht lesbar: {exc}")
        state = {}

    weather = _as_float(state.get("pv_yield_today_kwh"))
    result["weather_today_kwh"] = weather
    override = state.get("pv_forecast_override") or {}
    if override.get("active") and override.get("effective_kwh") is not None:
        result["expected_today_kwh"] = _as_float(override.get("effective_kwh"))
    else:
        result["expected_today_kwh"] = weather
    result["expected_tomorrow_kwh"] = _as_float(state.get("pv_yield_tomorrow_kwh"))

    result["produced_today_kwh"] = _produced_today_kwh(base_dir)
    if result["produced_today_kwh"] is None:
        result["produced_today_kwh"] = _as_float(state.get("live_pv_kwh_actual"))
    return result


def _stats_candidates(base_dir: str) -> List[Path]:
    paths: List[Path] = []
    try:
        from ram_cache import is_enabled, live_energy_path

        if is_enabled():
            paths.append(Path(live_energy_path(base_dir, "energy_stats_24h.json")))
    except Exception:
        pass
    paths.append(Path(base_dir) / "energy_monitor" / "energy_stats_24h.json")
    return paths


def _produced_today_kwh(base_dir: str) -> Optional[float]:
    today = date.today().isoformat()
    for path in _stats_candidates(base_dir):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("date") or "") != today:
            continue
        generation = data.get("generation_kwh") or data.get("generation") or {}
        produced = _as_float(generation.get("pv"))
        if produced is not None:
            return produced
    return None


def _load_curve(base_dir: str) -> Tuple[List[Dict[str, Any]], str]:
    from energy_timeseries_24h import load_timeseries_state

    state = load_timeseries_state(base_dir)
    samples = state.get("samples") or []
    day = str(state.get("date") or "")
    if samples:
        return list(samples), day
    disk = Path(base_dir) / "energy_monitor" / "energy_timeseries_24h.json"
    if not disk.is_file():
        return [], day
    try:
        raw = json.loads(disk.read_text(encoding="utf-8"))
    except Exception:
        return [], day
    if not isinstance(raw, dict):
        return [], day
    return list(raw.get("samples") or []), str(raw.get("date") or day)


def _canonical_point(sample: Dict[str, Any]) -> Optional[Point]:
    try:
        minute = int(sample.get("m"))
    except (TypeError, ValueError):
        return None
    if minute < 0 or minute >= 24 * 60:
        return None

    def pick(*keys: str) -> Optional[float]:
        for key in keys:
            value = _as_float(sample.get(key))
            if value is not None:
                return value
        return None

    return {
        "m": minute,
        "line_erzeugung": pick("line_erzeugung", "erzeugung"),
        "line_netz": pick("line_netz", "netz"),
        "line_verbrauch": pick("line_verbrauch", "betrieb"),
        "battery_kw": pick("battery_kw", "line_sofe2f"),
        "line_soc": pick("line_soc", "soc"),
    }


def _bin_points(points: Sequence[Point], bin_min: int = 5) -> List[Point]:
    buckets: Dict[int, List[Point]] = {}
    for point in points:
        slot = int(point["m"]) // bin_min * bin_min
        buckets.setdefault(slot, []).append(point)
    out: List[Point] = []
    keys = [spec["key"] for spec in _SERIES]
    for slot in sorted(buckets):
        rows = buckets[slot]
        merged: Point = {"m": slot}
        for key in keys:
            vals = [float(row[key]) for row in rows if row.get(key) is not None]
            merged[key] = sum(vals) / len(vals) if vals else None
        out.append(merged)
    return out


def _last_values(points: Sequence[Point]) -> Dict[str, Optional[float]]:
    last: Dict[str, Optional[float]] = {spec["key"]: None for spec in _SERIES}
    for point in points:
        for key in last:
            if point.get(key) is not None:
                last[key] = float(point[key])
    return last


def _pretty_date(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day or "heute"
    return parsed.strftime("%d.%m.%Y")


def _chart_date(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day or ""
    return parsed.strftime("%Y/%m/%d")


def _fmt_kw(value: float) -> str:
    if abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.replace(".", ",")


def format_chart_caption(
    day: str,
    last: Dict[str, Optional[float]],
    produced: Optional[float],
) -> str:
    lines = [f"Stromprofil {_pretty_date(day)}".strip()]
    labels = {
        "line_erzeugung": "Produktion",
        "line_netz": "Netz",
        "line_verbrauch": "Verbrauch",
        "battery_kw": "Batterie",
        "line_soc": "SOC",
    }
    for key, label in labels.items():
        value = last.get(key)
        if value is None:
            continue
        if key == "line_soc":
            lines.append(f"{label}: {value:.0f} %")
        else:
            lines.append(f"{label}: {_fmt_kw(value)} kW")
    if produced is not None:
        lines.append(f"Bisher produziert: {format_kwh(produced)}")
    return "\n".join(lines)


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = _FONT_BOLD if bold else _FONT_REGULAR
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _nice_step(span: float, count: int = 5) -> float:
    if span <= 0:
        return 1.0
    raw = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * magnitude
        if step >= raw * 0.85:
            return step
    return raw


def _ticks(lo: float, hi: float, count: int = 5) -> List[float]:
    step = _nice_step(hi - lo, count)
    start = math.ceil((lo - 1e-9) / step) * step
    values: List[float] = []
    cursor = start
    guard = 0
    while cursor <= hi + step * 0.05 and guard < 20:
        if cursor >= lo - step * 0.05:
            values.append(round(cursor, 6))
        cursor += step
        guard += 1
    return values


def render_stromprofil_png(
    samples: Sequence[Dict[str, Any]],
    day: str,
    produced_kwh: Optional[float] = None,
) -> Tuple[bytes, Dict[str, Optional[float]]]:
    """Zeichnet das Tages-Stromprofil und liefert PNG plus letzte Messwerte."""
    points = [p for raw in samples if (p := _canonical_point(raw))]
    if not points:
        raise ValueError("Keine Messwerte für das Stromprofil.")
    last = _last_values(points)
    if all(value is None for value in last.values()):
        raise ValueError("Keine Messwerte für das Stromprofil.")
    binned = _bin_points(points, 5)

    from PIL import Image, ImageDraw

    scale = 2
    width, height = 1400 * scale, 780 * scale
    image = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(image)
    title_font = _load_font(26 * scale, bold=True)
    sub_font = _load_font(15 * scale)
    legend_font = _load_font(15 * scale)
    axis_font = _load_font(13 * scale)

    margin_l = 78 * scale
    margin_r = 72 * scale
    margin_t = 156 * scale
    margin_b = 58 * scale
    plot_l, plot_t = margin_l, margin_t
    plot_r, plot_b = width - margin_r, height - margin_b
    plot_w = plot_r - plot_l
    plot_h = plot_b - plot_t

    ink = (17, 24, 39)
    muted = (100, 116, 139)
    grid = (226, 232, 240)
    zero = (148, 163, 184)

    draw.text((28 * scale, 22 * scale), "Stromprofil", font=title_font, fill=ink)
    date_label = _chart_date(day)
    if date_label:
        bbox = draw.textbbox((0, 0), date_label, font=sub_font)
        date_w = bbox[2] - bbox[0]
        draw.text((width - 28 * scale - date_w, 30 * scale), date_label, font=sub_font, fill=muted)
    if produced_kwh is not None:
        draw.text(
            (28 * scale, 62 * scale),
            f"Bisher produziert {format_kwh(produced_kwh)}",
            font=sub_font,
            fill=muted,
        )

    legend_x = 28 * scale
    legend_y = 100 * scale
    for spec in _SERIES:
        color = spec["color"]
        r = 6 * scale
        cy = legend_y + 8 * scale
        draw.ellipse((legend_x, cy - r, legend_x + 2 * r, cy + r), fill=color)
        label = spec["label"]
        draw.text((legend_x + 18 * scale, legend_y), label, font=legend_font, fill=ink)
        label_w = draw.textbbox((0, 0), label, font=legend_font)[2]
        legend_x += 18 * scale + label_w + 28 * scale

    kw_values = [
        float(point[spec["key"]])
        for point in binned
        for spec in _SERIES
        if spec["axis"] == "kw" and point.get(spec["key"]) is not None
    ]
    if not kw_values:
        ymin, ymax = -1.0, 1.0
    else:
        ymin = min(kw_values)
        ymax = max(kw_values)
        if ymin > 0:
            ymin = 0.0
        if ymax < 0:
            ymax = 0.0
        span = max(ymax - ymin, 1.0)
        pad = max(span * 0.08, 0.2)
        ymin -= pad
        ymax += pad
        if ymin < 0 < ymax:
            limit = max(abs(ymin), abs(ymax))
            ymin, ymax = -limit, limit

    def x_of(minute: float) -> float:
        return plot_l + (max(0.0, min(1440.0, minute)) / 1440.0) * plot_w

    def y_kw(value: float) -> float:
        return plot_b - ((value - ymin) / (ymax - ymin)) * plot_h

    def y_soc(value: float) -> float:
        clamped = max(0.0, min(100.0, value))
        return plot_b - (clamped / 100.0) * plot_h

    draw.rounded_rectangle(
        (plot_l, plot_t, plot_r, plot_b),
        radius=8 * scale,
        fill=(255, 255, 255),
        outline=(226, 232, 240),
        width=scale,
    )

    for tick in _ticks(ymin, ymax, 6):
        y = y_kw(tick)
        if y < plot_t + 2 or y > plot_b - 2:
            continue
        color = zero if abs(tick) < 1e-6 else grid
        draw.line([(plot_l, y), (plot_r, y)], fill=color, width=scale if abs(tick) < 1e-6 else max(1, scale // 2))
        label = f"{tick:.0f}" if abs(tick) >= 1 or abs(tick) < 1e-6 else f"{tick:.1f}"
        draw.text((12 * scale, y - 8 * scale), label, font=axis_font, fill=muted)

    for pct in (0, 25, 50, 75, 100):
        y = y_soc(pct)
        label = f"{pct}"
        bbox = draw.textbbox((0, 0), label, font=axis_font)
        draw.text((plot_r + 10 * scale, y - (bbox[3] - bbox[1]) / 2), label, font=axis_font, fill=muted)

    draw.text((12 * scale, plot_t - 18 * scale), "kW", font=axis_font, fill=muted)
    draw.text((plot_r + 8 * scale, plot_t - 18 * scale), "%", font=axis_font, fill=muted)

    for hour in range(0, 25, 3):
        x = x_of(hour * 60)
        draw.line([(x, plot_t), (x, plot_b)], fill=grid, width=max(1, scale // 2))
        label = f"{hour:02d}:00" if hour < 24 else "24:00"
        bbox = draw.textbbox((0, 0), label, font=axis_font)
        label_w = bbox[2] - bbox[0]
        draw.text((x - label_w / 2, plot_b + 12 * scale), label, font=axis_font, fill=muted)

    now_m = max(point["m"] for point in points)
    now_x = x_of(now_m)
    y = plot_t
    while y < plot_b:
        y2 = min(plot_b, y + 7 * scale)
        draw.line([(now_x, y), (now_x, y2)], fill=(148, 163, 184), width=scale)
        y = y2 + 5 * scale

    def segments(key: str, y_fn) -> List[List[Tuple[float, float]]]:
        segs: List[List[Tuple[float, float]]] = []
        current: List[Tuple[float, float]] = []
        prev_m: Optional[int] = None
        for point in binned:
            value = point.get(key)
            if value is None:
                if len(current) >= 2:
                    segs.append(current)
                current = []
                prev_m = None
                continue
            minute = int(point["m"])
            if prev_m is not None and minute - prev_m > 20:
                if len(current) >= 2:
                    segs.append(current)
                current = []
            current.append((x_of(minute), y_fn(float(value))))
            prev_m = minute
        if len(current) >= 2:
            segs.append(current)
        return segs

    for spec in _SERIES:
        y_fn = y_soc if spec["axis"] == "pct" else y_kw
        for seg in segments(spec["key"], y_fn):
            draw.line(seg, fill=spec["color"], width=spec["width"] * scale // 2, joint="curve")
        last_pt = None
        for point in reversed(binned):
            if point.get(spec["key"]) is not None:
                last_pt = point
                break
        if last_pt is not None:
            y_pos = y_fn(float(last_pt[spec["key"]]))
            x_pos = x_of(int(last_pt["m"]))
            radius = 5 * scale
            draw.ellipse(
                (x_pos - radius, y_pos - radius, x_pos + radius, y_pos + radius),
                fill=spec["color"],
                outline=(255, 255, 255),
                width=scale,
            )

    out = image.resize((width // scale, height // scale), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), last


def build_energiestatistik(base_dir: str) -> Tuple[bytes, str]:
    samples, day = _load_curve(base_dir)
    snapshot = load_energy_snapshot(base_dir)
    png, last = render_stromprofil_png(
        samples,
        day,
        produced_kwh=snapshot.get("produced_today_kwh"),
    )
    caption = format_chart_caption(day, last, snapshot.get("produced_today_kwh"))
    return png, caption
