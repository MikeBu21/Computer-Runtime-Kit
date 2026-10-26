# -*- coding: utf-8 -*-
"""Telegram-Befehle für PV-Ertrag und Stromprofil."""
from __future__ import annotations

import json
import math
from datetime import date
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


def classify_energy_command(text: str) -> Optional[str]:
    """today, tomorrow oder chart. Unbekannter Text ergibt None."""
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
