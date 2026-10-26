import asyncio
import fcntl
import html
import re
import subprocess
import os
import json
import time
import traceback
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters


BASE_DIR = Path(__file__).resolve().parent
EM_DIR = BASE_DIR / "energy_monitor"
JSON_PATH = BASE_DIR / "miners.json"
STATUS_PATH = BASE_DIR / "miner_status.json"
ENERGY_VALUE_PATH = EM_DIR / "energy_value.txt"
ENERGY_AVG_PATH = EM_DIR / "energy_avg.txt"
CHAT_IDS_PATH = BASE_DIR / 'telegram_chats.json'
CONFIG_PATH = BASE_DIR / 'telegram_config.json'

DEFAULT_FEED_IN_ALARM_KW = -95.0
DEFAULT_HIGH_USAGE_ALARM_KW = 50.0
LAST_ALARM_STATE = {
    "feed_in": False,
    "high_usage": False,
    "power_outage": False,
    "ha_data": False,
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_token():
    return load_config().get("telegram_token", "")


def load_alarm_settings():
    config = load_config()
    feed_in = safe_float(config.get("feed_in_alarm_kw"))
    high_usage = safe_float(config.get("high_usage_alarm_kw"))
    return (
        feed_in if feed_in is not None else DEFAULT_FEED_IN_ALARM_KW,
        high_usage if high_usage is not None else DEFAULT_HIGH_USAGE_ALARM_KW,
    )


def power_outage_enabled() -> bool:
    val = load_config().get("power_outage_enabled", False)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _config_flag(key: str, default: bool = False) -> bool:
    val = load_config().get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


async def _send_to_all_chats(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    for chat_id in load_chat_ids():
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            print(f"Telegram an {chat_id} fehlgeschlagen: {e}")


async def _send_burst(
    context: ContextTypes.DEFAULT_TYPE,
    messages: List[str],
    *,
    interval_seconds: float = 5.0,
) -> None:
    for i, msg in enumerate(messages):
        await _send_to_all_chats(context, msg)
        if i < len(messages) - 1:
            await asyncio.sleep(interval_seconds)


def load_miners():
    with JSON_PATH.open("r") as f:
        return json.load(f)


def load_status_data():
    data = []
    try:
        from ram_cache import is_enabled, read_json, setup_runtime_dirs

        setup_runtime_dirs(str(BASE_DIR))
        if is_enabled():
            ram_data = read_json(
                str(BASE_DIR),
                "miner_status.json",
                default=None,
                scope="backend",
            )
            if isinstance(ram_data, list) and ram_data:
                data = ram_data
    except Exception:
        pass
    if not data and STATUS_PATH.exists():
        try:
            with STATUS_PATH.open("r") as f:
                disk_data = json.load(f)
            if isinstance(disk_data, list) and disk_data:
                data = disk_data
        except Exception:
            data = []
    return data if isinstance(data, list) else []


def read_text_value(path, fallback="n/a"):
    try:
        from ram_cache import is_enabled, read_text

        if is_enabled():
            base_dir = BASE_DIR
            name = Path(path).name
            scope = "backend" if name == "miner_status.json" else "energy_monitor"
            val = read_text(base_dir, name, scope=scope)
            if val is not None:
                return val
    except Exception:
        pass
    try:
        return path.read_text().strip()
    except Exception:
        return fallback


def safe_float(value):
    try:
        return float(str(value).replace(",", ".").replace(" kW", "").replace("%", "").strip())
    except Exception:
        return None


def find_soc(value, key_hint=""):
    if isinstance(value, dict):
        preferred = (
            "soc", "SoC", "SOC", "battery_soc", "battery", "batterie",
            "value", "wert", "percent", "percentage", "prozent",
            "ladestand", "ladezustand", "speicher", "akku", "capacity",
        )
        for key in preferred:
            if key in value and value[key] not in (None, ""):
                found = find_soc(value[key], key)
                if found is not None:
                    return found
        if len(value) == 1:
            key, nested = next(iter(value.items()))
            return find_soc(nested, str(key))
        for key, nested in value.items():
            found = find_soc(nested, str(key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_soc(item, key_hint)
            if found is not None:
                return found
    elif value not in (None, ""):
        text = str(value).strip().replace("%", "").replace(",", ".")
        hint = key_hint.lower()
        if any(word in hint for word in ("soc", "battery", "batterie", "lade", "percent", "prozent", "value", "wert", "speicher", "akku", "capacity")):
            return text
        try:
            number = float(text)
            if 0 <= number <= 100:
                return str(int(number) if number.is_integer() else number)
        except Exception:
            pass
    return None


def load_battery_soc():
    candidates = [
        Path.home() / "daten.json",
        BASE_DIR / "daten.json",
        BASE_DIR / "energy_monitor" / "daten.json",
        BASE_DIR / "shared_settings" / "daten.json",
        BASE_DIR.parent / "daten.json",
    ]
    for root in (BASE_DIR.parent, BASE_DIR):
        if root.exists():
            for path in root.rglob("daten.json"):
                if path not in candidates:
                    candidates.append(path)

    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open("r") as f:
                data = json.load(f)
            soc = find_soc(data)
            if soc is not None:
                return soc
        except Exception:
            continue
    return "n/a"


def get_energy_summary():
    current = read_text_value(ENERGY_VALUE_PATH)
    avg = read_text_value(ENERGY_AVG_PATH)
    soc = load_battery_soc()
    return current, avg, soc


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _fmt_kw_line(label: str, value: Any, emoji: str = "") -> str:
    raw = str(value).strip() if value not in (None, "") else "n/a"
    if raw.lower() != "n/a":
        try:
            num = float(raw.replace(",", ".").replace(" kW", ""))
            raw = f"{num:.1f}"
        except ValueError:
            pass
    prefix = f"{emoji} " if emoji else ""
    return f"{prefix}<b>{_esc(label)}</b>  <code>{_esc(raw)} kW</code>"


def _has_flow_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    return str(value).strip().lower() != "n/a"


def _build_stromfluss_lines(flow: Dict[str, Any], soc: Any) -> list[str]:
    """Nur Felder anzeigen, die im Dashboard aktiv und mit Daten befüllt sind."""
    from dashboard_config import hidden_builtins, load_config as load_dash_config

    vis = flow.get("visibility") or {}
    hidden = hidden_builtins(load_dash_config())

    builtin_items = [
        ("pv", "pv", "PV-Platten", "☀️"),
        ("wasserkraft", "hydro", "Wasserkraft", "💧"),
        ("kelag", "grid", "Netz", "🔌"),
        ("wohnhaus", "haus", "Wohnhaus", "🏠"),
        ("betrieb", "betrieb_anzeige", "Betrieb (netto)", "🏭"),
    ]

    vis_key_map = {
        "pv": "pv",
        "wasserkraft": "hydro",
        "kelag": "grid",
        "wohnhaus": "haus",
        "betrieb": "betrieb",
    }

    lines: list[str] = []
    for builtin_key, flow_key, label, emoji in builtin_items:
        if builtin_key in hidden:
            continue
        if not vis.get(vis_key_map.get(builtin_key, builtin_key), True):
            continue
        value = flow.get(flow_key)
        if _has_flow_value(value):
            lines.append(_fmt_kw_line(label, value, emoji))

    if _has_flow_value(flow.get("verbrauch_gesamt")):
        lines.append(_fmt_kw_line("Verbrauch gesamt", flow.get("verbrauch_gesamt"), "🔌"))

    if "battery" not in hidden and vis.get("battery", True):
        soc_text = str(soc).strip() if soc not in (None, "") else ""
        if soc_text and soc_text.lower() != "n/a":
            lines.extend(_battery_power_lines(soc))

    for field_id, data in (flow.get("custom_fields") or {}).items():
        if not isinstance(data, dict):
            continue
        display = data.get("display") or data.get("value")
        if not _has_flow_value(display):
            continue
        icon = data.get("icon") or "⚡"
        name = data.get("name") or field_id
        lines.append(_fmt_kw_line(name, display, icon))

    return lines


def _alarm_row(active: bool, label: str, detail: str = "") -> str:
    icon = "✅" if active else "❌"
    extra = f" · <i>{detail}</i>" if detail else ""
    state = "aktiv" if active else "aus"
    return f"{icon} <b>{_esc(label)}</b>  <i>({state})</i>{extra}"


def _battery_power_lines(soc: Any) -> list[str]:
    """Rohwert aus Batteriescraper: + → laden, − → entladen."""
    from dashboard_battery import load_battery_power_kw

    lines = [f"🔋 <b>Batterie</b>  <code>{_esc(soc)}%</code>"]
    power = load_battery_power_kw()
    laden_txt = "—"
    entladen_txt = "—"
    if power is not None:
        if power > 0.25:
            laden_txt = f"{power:.1f} kW"
        elif power < -0.25:
            entladen_txt = f"{abs(power):.1f} kW"
    lines.append(f"⚡ <b>laden</b>  <code>{_esc(laden_txt)}</code>")
    lines.append(f"🔥 <b>entladen</b>  <code>{_esc(entladen_txt)}</code>")
    return lines


def _format_monthly_peak_line(peak: dict) -> str:
    peak_kw = peak.get("peak_kw")
    if peak_kw is None:
        return "—"
    peak_kwh = float(peak_kw) / 4.0
    text = f"{peak_kwh:.2f} kWh"
    peak_at = peak.get("peak_at")
    if peak_at:
        try:
            dt = datetime.fromisoformat(str(peak_at))
            text += f" ({dt.strftime('%d.%m.%Y %H:%M')})"
        except Exception:
            text += f" ({peak_at})"
    return text


def build_dashboard_status_html() -> str:
    """Oberer Status-Block: Alarme + Live-Werte wie Web-Dashboard."""
    from dashboard_battery import load_battery_data
    from miner_flow_data import build_flow_response
    from telegram_ha_alarm import build_dashboard_ha_status_lines, ha_monitor_enabled

    config = load_config()
    feed_in_kw, high_usage_kw = load_alarm_settings()
    outage_on = power_outage_enabled()
    ha_on = ha_monitor_enabled(config)

    bat = load_battery_data()
    flow = build_flow_response(
        str(BASE_DIR), battery_soc_loader=None, battery_power_kw=bat.get("power_kw")
    )
    soc = bat.get("soc") or load_battery_soc()

    lines = [
        "⚡ <b>MINING DASHBOARD</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🛡️ <b>Alarme &amp; Überwachung</b>",
        _alarm_row(ha_on, "Datenüberwachung / Home Assistant"),
        _alarm_row(outage_on, "Stromausfall-Überwachung"),
        _alarm_row(
            True,
            "Melder Netzbezug",
            f"Alarm wenn Netz > {high_usage_kw:g} kW",
        ),
        _alarm_row(
            True,
            "Melder Netzeinspeisung",
            f"Alarm wenn Netz ≤ {feed_in_kw:g} kW",
        ),
    ]

    lines.extend(build_dashboard_ha_status_lines(config))

    stromfluss = _build_stromfluss_lines(flow, soc)
    lines.append("📊 <b>Stromfluss (Live)</b>")
    if stromfluss:
        lines.extend(stromfluss)
    else:
        lines.append("<i>Keine aktiven Stromfluss-Felder</i>")

    return "\n".join(lines)


def _status_emoji(status_raw: str) -> str:
    if str(status_raw).lower() == "running":
        return "✅"
    if str(status_raw).lower() == "paused":
        return "⚠️"
    return "❌"


def _sorted_miners() -> List[Dict[str, Any]]:
    from control_algorithm.config import load_config, sort_miners_by_priority

    return sort_miners_by_priority(load_miners(), load_config())


def _sorted_ha_devices() -> List[Dict[str, Any]]:
    from control_algorithm.config import load_config, sort_ha_devices_by_priority
    from device_registry import load_ha_devices_raw

    return sort_ha_devices_by_priority(load_ha_devices_raw(), load_config())


def build_miner_status_text() -> str:
    miners = _sorted_miners()
    statuses = load_status_data()
    status_map = {entry.get("ip"): entry.get("status") for entry in statuses}

    lines = ["⛏️ <b>Miner Status</b>"]
    if not miners:
        lines.append("<i>Keine Miner konfiguriert</i>")
    else:
        for i, miner in enumerate(miners, 1):
            status_raw = status_map.get(miner["ip"], "unbekannt")
            note = _esc(miner.get("note", ""))
            model = _esc(miner.get("model", ""))
            detail = " · ".join(part for part in (model, note) if part)
            suffix = f" · {detail}" if detail else ""
            lines.append(
                f"{i}. <code>{_esc(miner['ip'])}</code> · {_status_emoji(status_raw)} "
                f"<b>{_esc(status_raw)}</b>{suffix}"
            )
    return "\n".join(lines)


def build_ha_devices_status_text() -> str:
    from device_registry import get_ha_device_status_map

    devices = _sorted_ha_devices()
    if not devices:
        return ""

    status_map = get_ha_device_status_map()
    lines = ["🔌 <b>Schaltbare Geräte</b>"]
    for i, dev in enumerate(devices, 1):
        entity_id = dev.get("entity_id", "")
        label = _esc(dev.get("label") or entity_id)
        status_raw = status_map.get(entity_id, "unbekannt")
        note = _esc(dev.get("note", ""))
        suffix = f" · {note}" if note else ""
        lines.append(
            f"{i}. <b>{label}</b> · {_status_emoji(status_raw)} "
            f"<b>{_esc(status_raw)}</b>{suffix}\n"
            f"   <code>{_esc(entity_id)}</code>"
        )
    return "\n".join(lines)


def _sorted_gpio_pins() -> List[Dict[str, Any]]:
    """Aktuelle Gibbo-Pins aus gpio_pins.json (Dashboard-Registry), sortiert nach Board-Pin."""
    from device_registry import load_gpio_pins_raw

    pins = [p for p in load_gpio_pins_raw() if isinstance(p, dict) and p.get("pin") is not None]
    return sorted(pins, key=lambda p: (int(p.get("pin") or 0), str(p.get("id") or "")))


def _gpio_pin_id(pin: Dict[str, Any]) -> str:
    pin_no = pin.get("pin")
    return str(pin.get("id") or (f"gpio:{pin_no}" if pin_no is not None else "")).strip()


def _gpio_pin_label(pin: Dict[str, Any]) -> str:
    return str(pin.get("label") or _gpio_pin_id(pin) or f"Pin {pin.get('pin')}").strip()


def build_gpio_pins_status_text() -> str:
    """Gibbo-Pins: running/paused wie Dashboard — dynamisch aus der Pin-Registry."""
    from device_registry import get_gpio_status_map

    pins = _sorted_gpio_pins()
    if not pins:
        return ""

    status_map = get_gpio_status_map()
    lines = ["🧷 <b>Schaltbare Gibbo-Pins</b>"]
    for i, pin in enumerate(pins, 1):
        pin_no = pin.get("pin")
        gid = _gpio_pin_id(pin)
        label = _esc(_gpio_pin_label(pin))
        status_raw = status_map.get(gid, "unbekannt")
        note = _esc(pin.get("note", ""))
        board = f"Board {pin_no}" if pin_no is not None else gid
        suffix = f" · {note}" if note else ""
        lines.append(
            f"{i}. <b>{label}</b> · <code>{_esc(board)}</code> · "
            f"{_status_emoji(status_raw)} <b>{_esc(status_raw)}</b>{suffix}"
        )
    return "\n".join(lines)


def build_devices_status_text() -> str:
    """Miner + HA-Geräte + Gibbo-Pins aus dem Dashboard."""
    parts = [build_miner_status_text()]
    ha_block = build_ha_devices_status_text()
    if ha_block:
        parts.append(ha_block)
    gpio_block = build_gpio_pins_status_text()
    if gpio_block:
        parts.append(gpio_block)
    return "\n\n".join(parts)


def _actor_from_update(update: Update) -> Dict[str, Any]:
    """Muss vor GPIO-/Miner-Schalt-Helfern stehen (wird dort referenziert)."""
    user = update.effective_user
    chat = update.effective_chat
    name = ""
    if user is not None:
        name = (user.full_name or user.username or "").strip()
    return {
        "source": "telegram",
        "chat_id": chat.id if chat is not None else None,
        "user_id": user.id if user is not None else None,
        "name": name,
    }


def _execute_gpio_pin(pin: Dict[str, Any], action: str, update: Update) -> bool:
    """pause/off → Aus, resume/on → Ein — wie Dashboard (commands.json → heizstap)."""
    from device_registry import execute_device_action

    gid = _gpio_pin_id(pin)
    pin_no = pin.get("pin")
    mapped = "pause" if str(action).lower() in ("pause", "off", "aus") else "resume"
    return bool(
        execute_device_action(
            {
                "type": "gpio",
                "id": gid,
                "ip": gid,
                "pin": pin_no,
                "label": _gpio_pin_label(pin),
            },
            mapped,
            source="manual",
            actor=_actor_from_update(update),
        )
    )


def _wait_gpio_status(gid: str, want: str, timeout_s: float = 1.6) -> str:
    import time

    from device_registry import get_gpio_status_map

    status = want
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = get_gpio_status_map().get(gid, want)
        if status == want:
            break
        time.sleep(0.2)
    return status


def load_chat_ids():
    if CHAT_IDS_PATH.exists():
        try:
            with CHAT_IDS_PATH.open("r") as f:
                data = json.load(f)
            return sorted({int(x) for x in data})
        except Exception:
            return []
    return []


def save_chat_id(chat_id):
    chat_ids = set(load_chat_ids())
    chat_ids.add(int(chat_id))
    with CHAT_IDS_PATH.open("w") as f:
        json.dump(sorted(chat_ids), f, indent=2)


def generate_command_list():
    miners = _sorted_miners()
    ha_devices = _sorted_ha_devices()
    gpio_pins = _sorted_gpio_pins()
    commands_msg = "[Befehle] (Nummer = Prio-Reihenfolge, 1 = höchste Prio)\n"
    for i, miner in enumerate(miners, 1):
        commands_msg += f"/pause{i}  -> Miner {miner['ip']}\n"
    for i, miner in enumerate(miners, 1):
        commands_msg += f"/resume{i} -> Miner {miner['ip']}\n"
    for i, dev in enumerate(ha_devices, 1):
        label = dev.get("label") or dev.get("entity_id", "")
        commands_msg += f"/ein{i}   -> {label}\n"
    for i, dev in enumerate(ha_devices, 1):
        label = dev.get("label") or dev.get("entity_id", "")
        commands_msg += f"/aus{i}   -> {label}\n"
    if gpio_pins:
        commands_msg += "/gibbopause  -> alle Gibbo-Pins pausieren (Aus)\n"
        commands_msg += "/gibboresume -> alle Gibbo-Pins starten (Ein)\n"
        for i, pin in enumerate(gpio_pins, 1):
            label = _gpio_pin_label(pin)
            board = pin.get("pin")
            commands_msg += f"/gibbopause{i}  -> {label} (Board {board})\n"
        for i, pin in enumerate(gpio_pins, 1):
            label = _gpio_pin_label(pin)
            board = pin.get("pin")
            commands_msg += f"/gibboresume{i} -> {label} (Board {board})\n"
        commands_msg += "Text: Gibbo pause / Gibbo resume (alle Pins)\n"
    commands_msg += "/ertrag -> Ertrag heute (produziert und geschätzt)\n"
    commands_msg += "/morgen -> Ertrag morgen geschätzt\n"
    commands_msg += "/statistik -> Stromprofil\n"
    commands_msg += "/status -> Bericht Miner, Geräte und Gibbo-Pins"
    return commands_msg


def energy_reply_markup() -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton("Ertrag heute"),
            KeyboardButton("Ertrag morgen"),
        ],
        [KeyboardButton("Stromprofil")],
        [KeyboardButton("Status")],
    ]
    if _sorted_gpio_pins():
        rows.append(
            [
                KeyboardButton("Gibbo pause"),
                KeyboardButton("Gibbo resume"),
            ]
        )
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        text = generate_command_list()
        await update.message.reply_text(text, reply_markup=energy_reply_markup())
    except Exception as e:
        await update.message.reply_text(f"Fehler bei /start: {e}")


async def _reply_energy(update: Update, kind: str) -> None:
    save_chat_id(update.effective_chat.id)
    message = update.effective_message
    if message is None:
        return
    from telegram_energy import (
        build_energiestatistik,
        format_today_message,
        format_tomorrow_message,
        load_energy_snapshot,
    )

    markup = energy_reply_markup()
    try:
        if kind == "chart":
            png, caption = build_energiestatistik(str(BASE_DIR))
            photo = BytesIO(png)
            photo.name = "stromprofil.png"
            await message.reply_photo(photo=photo, caption=caption, reply_markup=markup)
            return
        snapshot = load_energy_snapshot(str(BASE_DIR))
        if kind == "tomorrow":
            text = format_tomorrow_message(snapshot.get("expected_tomorrow_kwh"))
        else:
            text = format_today_message(
                snapshot.get("expected_today_kwh"),
                snapshot.get("produced_today_kwh"),
                snapshot.get("weather_today_kwh"),
            )
        await message.reply_text(text, reply_markup=markup)
    except Exception as e:
        print(f"Energie-Befehl fehlgeschlagen: {e}")
        traceback.print_exc()
        await message.reply_text(
            "Die Energiedaten konnten gerade nicht gelesen werden.",
            reply_markup=markup,
        )


async def energy_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_energy(update, "today")


async def energy_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_energy(update, "tomorrow")


async def energy_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_energy(update, "chart")


async def energy_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram_energy import classify_energy_command

    text = (update.message.text or "").strip()
    low = text.lower()
    if low == "status":
        await status(update, context)
        return
    if re.match(r"(?i)^gibbo\s*(pause|aus|off)(\s*\d+)?$", text):
        await gibbo_pause(update, context)
        return
    if re.match(r"(?i)^gibbo\s*(resume|ein|on|start)(\s*\d+)?$", text):
        await gibbo_resume(update, context)
        return
    kind = classify_energy_command(text)
    if not kind:
        return
    await _reply_energy(update, kind)


def _parse_gibbo_index(text: str) -> Optional[int]:
    """None = alle Pins; sonst 1-basierter Index aus Registry-Reihenfolge."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*$", text.strip())
    if not m:
        return None
    return int(m.group(1))


async def _gibbo_control(update: Update, action: str) -> None:
    """action: pause|resume — dynamisch alle oder einen Pin aus gpio_pins.json."""
    save_chat_id(update.effective_chat.id)
    pins = _sorted_gpio_pins()
    if not pins:
        await update.message.reply_text("Keine Gibbo-Pins konfiguriert.")
        await update.message.reply_text(generate_command_list())
        return

    text = update.message.text or ""
    idx = _parse_gibbo_index(text)
    want = "paused" if action == "pause" else "running"
    verb = "pausiert" if action == "pause" else "gestartet"

    targets: List[Dict[str, Any]]
    if idx is None:
        targets = pins
    else:
        if idx < 1 or idx > len(pins):
            await update.message.reply_text(f"Gibbo-Pin {idx} nicht gefunden.")
            await update.message.reply_text(generate_command_list())
            return
        targets = [pins[idx - 1]]

    lines = []
    for pin in targets:
        label = _gpio_pin_label(pin)
        gid = _gpio_pin_id(pin)
        ok = _execute_gpio_pin(pin, action, update)
        if ok:
            status = _wait_gpio_status(gid, want)
            lines.append(f"{'✅' if status == want else '⚠️'} {label}: {status}")
        else:
            lines.append(f"❌ {label}: konnte nicht {verb} werden")

    await update.message.reply_text("\n".join(lines) or f"Gibbo {verb}.")
    gpio_block = build_gpio_pins_status_text()
    if gpio_block:
        await update.message.reply_text(gpio_block, parse_mode="HTML")
    await update.message.reply_text(
        generate_command_list(),
        reply_markup=energy_reply_markup(),
    )


async def gibbo_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _gibbo_control(update, "pause")
    except Exception as e:
        await update.message.reply_text(f"Fehler bei Gibbo pause: {e}")
        await update.message.reply_text(generate_command_list())


async def gibbo_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _gibbo_control(update, "resume")
    except Exception as e:
        await update.message.reply_text(f"Fehler bei Gibbo resume: {e}")
        await update.message.reply_text(generate_command_list())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        header = build_dashboard_status_html()
        devices_block = build_devices_status_text()
        await update.message.reply_text(header, parse_mode="HTML")
        await update.message.reply_text(devices_block, parse_mode="HTML")
        await update.message.reply_text(
            generate_command_list(),
            reply_markup=energy_reply_markup(),
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler bei Status: {e}")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        match = re.search(r"\d+$", update.message.text or "")
        if not match:
            raise ValueError("Nummer fehlt")
        num = int(match.group())
        miners = _sorted_miners()
        if num < 1 or num > len(miners):
            await update.message.reply_text(f"Miner {num} nicht gefunden.")
            await update.message.reply_text(generate_command_list())
            return

        from device_registry import execute_device_action

        miner = miners[num - 1]
        device = {
            "type": "miner",
            "ip": miner["ip"],
            "label": miner.get("note") or miner.get("model") or miner["ip"],
            "username": miner.get("username", "root"),
            "password": miner.get("password", ""),
        }
        ok = execute_device_action(
            device, "pause", source="manual", actor=_actor_from_update(update)
        )
        if ok:
            await update.message.reply_text(f"Miner {num} pausiert ({miner['ip']}).")
        else:
            await update.message.reply_text(f"Miner {num} konnte nicht pausiert werden.")
        await update.message.reply_text(generate_command_list())
    except Exception as e:
        await update.message.reply_text(f"Fehler bei /pause: {e}")
        await update.message.reply_text(generate_command_list())


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        match = re.search(r"\d+$", update.message.text or "")
        if not match:
            raise ValueError("Nummer fehlt")
        num = int(match.group())
        miners = _sorted_miners()
        if num < 1 or num > len(miners):
            await update.message.reply_text(f"Miner {num} nicht gefunden.")
            await update.message.reply_text(generate_command_list())
            return

        from device_registry import execute_device_action

        miner = miners[num - 1]
        device = {
            "type": "miner",
            "ip": miner["ip"],
            "label": miner.get("note") or miner.get("model") or miner["ip"],
            "username": miner.get("username", "root"),
            "password": miner.get("password", ""),
        }
        ok = execute_device_action(
            device, "resume", source="manual", actor=_actor_from_update(update)
        )
        if ok:
            await update.message.reply_text(f"Miner {num} gestartet ({miner['ip']}).")
        else:
            await update.message.reply_text(f"Miner {num} konnte nicht gestartet werden.")
        await update.message.reply_text(generate_command_list())
    except Exception as e:
        await update.message.reply_text(f"Fehler bei /resume: {e}")
        await update.message.reply_text(generate_command_list())


def _ha_device_by_index(num: int):
    devices = _sorted_ha_devices()
    if num < 1 or num > len(devices):
        return None, devices
    return devices[num - 1], devices


async def ha_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        match = re.search(r"\d+$", update.message.text or "")
        if not match:
            raise ValueError("Nummer fehlt")
        num = int(match.group())
        dev, devices = _ha_device_by_index(num)
        if dev is None:
            await update.message.reply_text(f"Gerät {num} nicht gefunden.")
            await update.message.reply_text(generate_command_list())
            return

        from device_registry import execute_device_action

        label = dev.get("label") or dev.get("entity_id", "")
        ok = execute_device_action(
            {"type": "ha", "entity_id": dev["entity_id"], "label": label},
            "on",
            source="manual",
            actor=_actor_from_update(update),
        )
        if ok:
            await update.message.reply_text(f"✅ {label} eingeschaltet.")
        else:
            await update.message.reply_text(f"❌ {label} konnte nicht eingeschaltet werden.")
        await update.message.reply_text(generate_command_list())
    except Exception as e:
        await update.message.reply_text(f"Fehler bei /ein: {e}")
        await update.message.reply_text(generate_command_list())


async def ha_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        save_chat_id(update.effective_chat.id)
        match = re.search(r"\d+$", update.message.text or "")
        if not match:
            raise ValueError("Nummer fehlt")
        num = int(match.group())
        dev, devices = _ha_device_by_index(num)
        if dev is None:
            await update.message.reply_text(f"Gerät {num} nicht gefunden.")
            await update.message.reply_text(generate_command_list())
            return

        from device_registry import execute_device_action

        label = dev.get("label") or dev.get("entity_id", "")
        ok = execute_device_action(
            {"type": "ha", "entity_id": dev["entity_id"], "label": label},
            "off",
            source="manual",
            actor=_actor_from_update(update),
        )
        if ok:
            await update.message.reply_text(f"✅ {label} ausgeschaltet.")
        else:
            await update.message.reply_text(f"❌ {label} konnte nicht ausgeschaltet werden.")
        await update.message.reply_text(generate_command_list())
    except Exception as e:
        await update.message.reply_text(f"Fehler bei /aus: {e}")
        await update.message.reply_text(generate_command_list())


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("Befehl nicht erkannt.", reply_markup=energy_reply_markup())
    await update.message.reply_text(generate_command_list(), reply_markup=energy_reply_markup())


def _dashboard_control_values() -> Dict[str, Any]:
    from dashboard_control import get_control_values

    return get_control_values(str(BASE_DIR)).get("values") or {}


async def energy_alarm_job(context: ContextTypes.DEFAULT_TYPE):
    from dashboard_control import get_grid_kw_for_control

    current = safe_float(get_grid_kw_for_control(str(BASE_DIR)))
    if current is None:
        current = safe_float(read_text_value(ENERGY_VALUE_PATH, ""))
    if current is None:
        return

    feed_in_alarm_kw, high_usage_alarm_kw = load_alarm_settings()
    alerts = []

    if current <= feed_in_alarm_kw:
        if not LAST_ALARM_STATE["feed_in"]:
            alerts.append(
                f"⚠️ Achtung Einspeisung: Netz {current:g} kW (Grenze ≤ {feed_in_alarm_kw:g} kW)"
            )
            LAST_ALARM_STATE["feed_in"] = True
    else:
        LAST_ALARM_STATE["feed_in"] = False

    if current > high_usage_alarm_kw:
        if not LAST_ALARM_STATE["high_usage"]:
            alerts.append(
                f"⚠️ Achtung Netzbezug über {high_usage_alarm_kw:g} kW: aktuell {current:g} kW"
            )
            LAST_ALARM_STATE["high_usage"] = True
    else:
        LAST_ALARM_STATE["high_usage"] = False

    if not alerts:
        return

    for chat_id in load_chat_ids():
        for alert in alerts:
            try:
                await context.bot.send_message(chat_id=chat_id, text=alert)
            except Exception as e:
                print(f"Alarm konnte nicht an {chat_id} gesendet werden: {e}")


async def power_outage_job(context: ContextTypes.DEFAULT_TYPE):
    if not power_outage_enabled():
        LAST_ALARM_STATE["power_outage"] = False
        return

    from power_outage_detect import check_power_outage

    result = check_power_outage(str(BASE_DIR))
    chat_ids = load_chat_ids()
    if not chat_ids:
        return

    if result.is_outage:
        if not LAST_ALARM_STATE["power_outage"]:
            text = (
                "🔴 STROMAUSFALL VERMUTET\n"
                f"{result.reason}\n"
                "Bitte Anlage und Netz vor Ort prüfen."
            )
            for chat_id in chat_ids:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    print(f"Stromausfall-Alarm an {chat_id} fehlgeschlagen: {e}")
            LAST_ALARM_STATE["power_outage"] = True
        return

    if LAST_ALARM_STATE["power_outage"]:
        text = "✅ Stromversorgung / Monitoring wieder aktiv."
        for chat_id in chat_ids:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                print(f"Strom-Wieder-OK an {chat_id} fehlgeschlagen: {e}")
    LAST_ALARM_STATE["power_outage"] = False


async def ha_data_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Home-Assistant-Datenüberwachung — Alarm bei instabiler Verbindung."""
    from telegram_ha_alarm import evaluate_ha_telegram_alarm, ha_monitor_enabled

    config = load_config()
    if not ha_monitor_enabled(config):
        LAST_ALARM_STATE["ha_data"] = False
        return

    chat_ids = load_chat_ids()
    if not chat_ids:
        return

    alarm, recovery, use_html = evaluate_ha_telegram_alarm(config)
    parse_mode = "HTML" if use_html else None

    if alarm:
        if not LAST_ALARM_STATE["ha_data"]:
            for chat_id in chat_ids:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=alarm, parse_mode=parse_mode
                    )
                except Exception as e:
                    print(f"HA-Alarm an {chat_id} fehlgeschlagen: {e}")
            LAST_ALARM_STATE["ha_data"] = True
        return

    if recovery:
        for chat_id in chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=recovery, parse_mode=parse_mode
                )
            except Exception as e:
                print(f"HA-OK an {chat_id} fehlgeschlagen: {e}")
    LAST_ALARM_STATE["ha_data"] = False


def _recovery_callback_key(mac: str, exists: bool) -> str:
    from ip_recovery import normalize_mac as norm_mac

    mac_hex = (norm_mac(mac) or mac or "").replace(":", "")
    return f"rec:{'y' if exists else 'n'}:{mac_hex[:32]}"


async def recovery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    save_chat_id(query.message.chat_id if query.message else update.effective_chat.id)
    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "rec":
        return
    exists = parts[1] == "y"
    mac_key = parts[2]
    try:
        from ip_recovery import confirm_miner_exists, load_miners, normalize_mac

        target = None
        for m in load_miners():
            mac_n = normalize_mac(m.get("mac")) or ""
            if mac_n.replace(":", "") == mac_key:
                target = m.get("ip")
                break
        if not target:
            await query.edit_message_text("Miner nicht mehr in der Liste.")
            return
        ok, message = confirm_miner_exists(str(target), exists=exists)
        await query.edit_message_text(message if ok else f"Fehler: {message}")
    except Exception as exc:
        await query.edit_message_text(f"Fehler: {exc}")


async def ip_recovery_notification_job(context: ContextTypes.DEFAULT_TYPE):
    from ip_recovery import pop_notifications
    from ip_recovery import normalize_mac as norm_mac

    chat_ids = load_chat_ids()
    if not chat_ids:
        pop_notifications()
        return

    for note in pop_notifications():
        kind = note.get("kind")
        payload = note.get("payload") or {}
        text = payload.get("message") or ""
        if not text:
            continue
        reply_markup = None
        if kind == "device_missing":
            mac = payload.get("mac") or ""
            if mac and mac != "unbekannt":
                mac_n = norm_mac(mac) or mac
                reply_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Ja, Gerät existiert",
                                callback_data=_recovery_callback_key(mac_n, True),
                            ),
                            InlineKeyboardButton(
                                "Nein, entfernen",
                                callback_data=_recovery_callback_key(mac_n, False),
                            ),
                        ]
                    ]
                )
        for chat_id in chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except Exception as e:
                print(f"IP-Recovery-Hinweis an {chat_id} fehlgeschlagen: {e}")


def _acquire_singleton_lock():
    """Verhindert doppelte getUpdates (Conflict) durch zweite minerbot-Instanz."""
    lock_path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "minerbot.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        lock_path = Path("/tmp/minerbot.lock")
    fp = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("❌ minerbot läuft bereits (Lock). Zweite Instanz beendet.")
        fp.close()
        raise SystemExit(0)
    fp.seek(0)
    fp.truncate()
    fp.write(str(os.getpid()))
    fp.flush()
    return fp


async def _bot_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut, RetryAfter)):
        print(f"[Warnung] Telegram-Netzwerk: {err}")
        return
    print(f"[Fehler] Bot-Handler: {err}")
    traceback.print_exception(type(err), err, err.__traceback__)


def _build_application(token: str) -> Application:
    try:
        from telegram.ext import JobQueue

        app = Application.builder().token(token).job_queue(JobQueue()).build()
    except Exception as e:
        print(f"[Warnung] JobQueue-Setup: {e}")
        app = Application.builder().token(token).build()

    app.add_error_handler(_bot_error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler(["ertrag", "ertrag_heute", "ertragheute"], energy_today))
    app.add_handler(CommandHandler(["morgen", "ertrag_morgen", "ertragmorgen"], energy_tomorrow))
    app.add_handler(CommandHandler(["statistik", "energiestatistik"], energy_chart))
    app.add_handler(MessageHandler(filters.Regex(r"^/pause\d+$"), pause))
    app.add_handler(MessageHandler(filters.Regex(r"^/resume\d+$"), resume))
    app.add_handler(MessageHandler(filters.Regex(r"^/ein\d+$"), ha_on))
    app.add_handler(MessageHandler(filters.Regex(r"^/aus\d+$"), ha_off))
    app.add_handler(CommandHandler(["gibbopause", "gibbo_pause"], gibbo_pause))
    app.add_handler(CommandHandler(["gibboresume", "gibbo_resume"], gibbo_resume))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/gibbopause\d+$"), gibbo_pause))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/gibboresume\d+$"), gibbo_resume))
    app.add_handler(
        MessageHandler(
            filters.Regex(
                r"(?i)^(ertrag heute|ertrag morgen|stromprofil|status|ertrag heute erwartet|ertrag morgen erwartet|energiestatistik|energie statistik|gibbo\s*(pause|resume|aus|ein|off|on|start)(\s*\d+)?)$"
            ),
            energy_button,
        )
    )
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(CallbackQueryHandler(recovery_callback, pattern=r"^rec:"))

    if app.job_queue is not None:
        app.job_queue.run_repeating(energy_alarm_job, interval=30, first=10)
        app.job_queue.run_repeating(power_outage_job, interval=15, first=12)
        app.job_queue.run_repeating(ha_data_monitor_job, interval=30, first=14)
        app.job_queue.run_repeating(ip_recovery_notification_job, interval=20, first=8)
        print("[Info] JobQueue aktiv – Energie-/HA-Alarme laufen.")
    else:
        print("⚠️ JobQueue nicht verfügbar, automatische Energie-Alarme sind deaktiviert.")
    return app


def start_bot():
    token = load_token()
    if not token:
        print("❌ Kein Telegram-Bot-Token gefunden. Bitte über das Webinterface setzen.")
        return

    lock_fp = _acquire_singleton_lock()

    try:
        from ram_cache import setup_runtime_dirs

        setup_runtime_dirs(str(BASE_DIR))
        print("[Info] RAM-Cache hydratisiert (Status aus SD falls nötig)")
    except Exception as e:
        print(f"[Warnung] RAM-Hydrate: {e}")

    # Netzwerk-/API-Abstürze (Bad Gateway, DNS) → kurze Pause und Neustart
    backoff = 5
    while True:
        try:
            app = _build_application(token)
            print("✅ Bot läuft ... mit STRG+C beenden.")
            app.run_polling(
                drop_pending_updates=True,
                close_loop=False,
            )
            backoff = 5
        except (NetworkError, TimedOut, RetryAfter) as e:
            print(f"[Warnung] Polling-Netzwerkfehler: {e} – Neustart in {backoff}s")
        except KeyboardInterrupt:
            print("[Info] Bot beendet (STRG+C).")
            break
        except SystemExit:
            raise
        except Exception as e:
            print(f"[Fehler] Bot abgestürzt: {e}")
            traceback.print_exc()
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)
        token = load_token() or token

    try:
        lock_fp.close()
    except Exception:
        pass


if __name__ == "__main__":
    start_bot()
