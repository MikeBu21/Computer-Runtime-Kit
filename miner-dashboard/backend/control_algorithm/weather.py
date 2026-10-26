# -*- coding: utf-8 -*-
"""Open-Meteo Geocoding und Wettervorhersage."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def geocode_city(city: str, country: str = "") -> Tuple[Optional[float], Optional[float], str]:
    """
    Stadt → (lat, lon, display_name).
    Raises ValueError wenn nicht gefunden.
    """
    city = (city or "").strip()
    if not city:
        raise ValueError("Bitte eine Stadt eingeben.")
    params = {"name": city, "count": 5, "language": "de", "format": "json"}
    if country:
        params["country"] = country
    url = GEOCODING_URL + "?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Stadt „{city}“ wurde nicht gefunden.")
    best = results[0]
    lat = float(best["latitude"])
    lon = float(best["longitude"])
    name = best.get("name", city)
    admin = best.get("admin1") or ""
    country_name = best.get("country") or ""
    display = ", ".join(x for x in (name, admin, country_name) if x)
    return lat, lon, display


def fetch_forecast(lat: float, lon: float, timezone: str = "Europe/Vienna") -> Dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "weather_code,cloud_cover,shortwave_radiation,sunshine_duration",
        "timezone": timezone,
        "forecast_days": 2,
    }
    url = FORECAST_URL + "?" + urllib.parse.urlencode(params)
    return _fetch_json(url)


def _fetch_json(url: str, timeout: int = 8) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "miner-dashboard-control-algo/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Wetter-API Fehler: {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Wetter-API nicht erreichbar: {e.reason}") from e
