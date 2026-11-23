"""Météo des sites via Open-Meteo (sans clé API) : 3 jours passés + 16 jours de prévisions."""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

import httpx

from ..config import settings

log = logging.getLogger("weather")

TTL = 3600
MAX_CACHE = 2000
_cache: dict[tuple[float, float], tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()

_WMO = {
    0: ("Ciel dégagé", "☀️"), 1: ("Plutôt dégagé", "🌤️"), 2: ("Partiellement nuageux", "⛅"),
    3: ("Couvert", "☁️"), 45: ("Brouillard", "🌫️"), 48: ("Brouillard givrant", "🌫️"),
    51: ("Bruine légère", "🌦️"), 53: ("Bruine", "🌦️"), 55: ("Bruine dense", "🌧️"),
    56: ("Bruine verglaçante", "🌧️"), 57: ("Bruine verglaçante", "🌧️"),
    61: ("Pluie faible", "🌦️"), 63: ("Pluie", "🌧️"), 65: ("Pluie forte", "🌧️"),
    66: ("Pluie verglaçante", "🌧️"), 67: ("Pluie verglaçante", "🌧️"),
    71: ("Neige faible", "🌨️"), 73: ("Neige", "🌨️"), 75: ("Neige forte", "❄️"), 77: ("Grains de neige", "🌨️"),
    80: ("Averses", "🌦️"), 81: ("Averses", "🌧️"), 82: ("Fortes averses", "⛈️"),
    85: ("Averses de neige", "🌨️"), 86: ("Averses de neige", "❄️"),
    95: ("Orage", "⛈️"), 96: ("Orage et grêle", "⛈️"), 99: ("Orage et grêle", "⛈️"),
}


def describe(code: int | None) -> tuple[str, str]:
    return _WMO.get(code, ("Inconnu", "❔"))


def verdict(day: dict, prev_rain: float) -> tuple[str, str]:
    """Évalue la journée pour grimper : ('ok'|'warn'|'bad', message)."""
    issues: list[tuple[int, str]] = []
    rain = day["rain"] or 0
    prob = day["rain_prob"] or 0
    code = day["code"] or 0
    if rain >= 5 or code >= 95:
        issues.append((2, "Pluie ou orage : sortie déconseillée"))
    elif rain >= 1 or prob >= 60:
        issues.append((1, "Risque de pluie"))
    if (day["wind"] or 0) >= 50:
        issues.append((2, "Vent très fort"))
    elif (day["wind"] or 0) >= 35:
        issues.append((1, "Vent fort"))
    if prev_rain >= 5:
        issues.append((1, "Rocher probablement humide (pluie des jours précédents)"))
    if (day["tmax"] or 0) >= 32:
        issues.append((1, "Forte chaleur"))
    if day["tmax"] is not None and day["tmax"] <= 2:
        issues.append((1, "Grand froid"))
    if not issues:
        return "ok", "Bonnes conditions"
    level, text = max(issues, key=lambda i: i[0])
    return ("bad" if level == 2 else "warn"), text


def parse_daily(payload: dict) -> list[dict]:
    d = payload["daily"]
    days: list[dict] = []
    for i, iso in enumerate(d["time"]):
        code = d["weather_code"][i]
        label, icon = describe(code)
        days.append({
            "date": date.fromisoformat(iso), "code": code, "label": label, "icon": icon,
            "tmin": d["temperature_2m_min"][i], "tmax": d["temperature_2m_max"][i],
            "rain": d["precipitation_sum"][i], "rain_prob": d["precipitation_probability_max"][i],
            "wind": d["wind_speed_10m_max"][i],
        })
    for i, day in enumerate(days):
        prev = sum((p["rain"] or 0) for p in days[max(0, i - 2):i])
        day["verdict"], day["verdict_text"] = verdict(day, prev)
    for i, day in enumerate(days):
        # La veille (passée ou prévue) : c'est surtout sa pluie qui dit si le rocher sera sec.
        # Copie des seuls champs utiles : pas de référence circulaire entre les jours.
        eve = days[i - 1] if i > 0 else None
        day["eve"] = {k: eve[k] for k in ("date", "code", "label", "icon", "tmin", "tmax", "rain", "rain_prob", "wind")} \
            if eve else None
    return days


def get_forecast(lat: float, lon: float) -> list[dict] | None:
    """Prévisions journalières (None si le service est indisponible)."""
    key = (round(lat, 2), round(lon, 2))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    try:
        r = httpx.get(
            settings.weather_url,
            params={
                "latitude": key[0], "longitude": key[1], "timezone": settings.timezone,
                "past_days": 3, "forecast_days": 16,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                         "precipitation_probability_max,wind_speed_10m_max",
            },
            headers={"User-Agent": settings.user_agent},
            timeout=8,
        )
        r.raise_for_status()
        days = parse_daily(r.json())
    except Exception as exc:  # noqa: BLE001 - la météo ne doit jamais casser une page
        log.warning("Météo indisponible (%s)", exc)
        with _cache_lock:  # on ressert une valeur périmée plutôt que rien
            hit = _cache.get(key)
        return hit[1] if hit else None
    with _cache_lock:
        if len(_cache) >= MAX_CACHE:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, days)
    return days


def forecast_for_day(lat: float, lon: float, day: date) -> dict | None:
    days = get_forecast(lat, lon)
    return next((d for d in days or [] if d["date"] == day), None)
