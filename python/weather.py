"""Previsioni meteo da Open-Meteo (gratuito, senza chiave), con cache su disco."""

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import config

log = logging.getLogger("meteo")

API_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_VARS = "precipitation,cloud_cover,wind_gusts_10m,wind_direction_10m"
HOURLY_VARS = ("precipitation,precipitation_probability,cloud_cover,"
               "direct_normal_irradiance,diffuse_radiation,wind_gusts_10m,wind_direction_10m")
RETRY_AFTER_ERROR_S = 5 * 60


@dataclass
class Hour:
    """Valori riferiti all'ora che TERMINA a `end` (convenzione Open-Meteo)."""
    end: float
    precip_mm: float
    precip_prob: float | None
    cloud: float
    dni: float
    dhi: float
    gust_ms: float
    wind_dir: float


@dataclass
class Weather:
    fetched_at: float
    rain_rate_mm_h: float = 0.0
    cloud: float = 0.0
    gust_ms: float = 0.0
    wind_dir: float = 180.0
    hours: list[Hour] = field(default_factory=list)

    def hour_at(self, t: float) -> Hour | None:
        for h in self.hours:
            if h.end - 3600.0 < t <= h.end:
                return h
        return None

    def irradiance(self, t: float) -> tuple[float, float] | None:
        h = self.hour_at(t)
        return (h.dni, h.dhi) if h else None

    def gust(self, t: float) -> float:
        h = self.hour_at(t)
        return max(self.gust_ms, h.gust_ms) if h else self.gust_ms

    def raining_now(self) -> bool:
        return self.rain_rate_mm_h >= config.RAIN_NOW_MM_H

    def rain_soon(self, t: float) -> bool:
        """Pioggia probabile entro la prossima mezz'ora circa."""
        h = self.hour_at(t + 1800.0)
        if h is None or h.precip_mm < config.RAIN_SOON_MM:
            return False
        return h.precip_prob is None or h.precip_prob >= config.RAIN_SOON_PROB

    def rain_within(self, t: float, hours: float) -> tuple[float, float | None]:
        """(mm totali attesi, secondi alla prima ora piovosa) nell'orizzonte dato."""
        total, first = 0.0, None
        for h in self.hours:
            if h.end <= t or h.end - 3600.0 > t + hours * 3600.0:
                continue
            if h.precip_prob is not None and h.precip_prob < 50.0:
                continue
            total += h.precip_mm
            if first is None and h.precip_mm >= 0.2:
                first = max(0.0, h.end - 3600.0 - t)
        return total, first


def parse(payload: dict, fetched_at: float) -> Weather:
    cur = payload.get("current") or {}
    interval = float(cur.get("interval") or 900.0)
    w = Weather(
        fetched_at=fetched_at,
        rain_rate_mm_h=float(cur.get("precipitation") or 0.0) * 3600.0 / interval,
        cloud=float(cur.get("cloud_cover") or 0.0),
        gust_ms=float(cur.get("wind_gusts_10m") or 0.0),
        wind_dir=float(cur.get("wind_direction_10m") or 180.0),
    )
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    def col(name):
        values = hourly.get(name) or []
        return values if len(values) == len(times) else [None] * len(times)

    cols = [col(n) for n in ("precipitation", "precipitation_probability", "cloud_cover",
                             "direct_normal_irradiance", "diffuse_radiation",
                             "wind_gusts_10m", "wind_direction_10m")]
    for i, ts in enumerate(times):
        pr, pp, cc, dni, dhi, gust, wdir = (c[i] for c in cols)
        w.hours.append(Hour(
            end=float(ts),
            precip_mm=float(pr or 0.0),
            precip_prob=None if pp is None else float(pp),
            cloud=float(cc or 0.0),
            dni=float(dni or 0.0),
            dhi=float(dhi or 0.0),
            gust_ms=float(gust or 0.0),
            wind_dir=float(wdir if wdir is not None else 180.0),
        ))
    return w


class WeatherService:
    def __init__(self, lat: float, lon: float, fetch=None):
        self._lat, self._lon = lat, lon
        self._fetch = fetch or self._http_fetch
        self._cache_path = os.path.join(config.DATA_DIR, "weather.json")
        self._weather: Weather | None = None
        self._next_try = 0.0
        self._load_cache()

    def get(self, now: float) -> Weather | None:
        """Ultima previsione valida, aggiornandola se e' ora. None se troppo vecchia."""
        if now >= self._next_try:
            self._refresh(now)
        w = self._weather
        if w is None or now - w.fetched_at > config.WEATHER_MAX_AGE_S:
            return None
        return w

    def _refresh(self, now: float) -> None:
        try:
            payload = self._fetch()
            self._weather = parse(payload, now)
            self._next_try = now + config.WEATHER_REFRESH_S
            self._save_cache(payload, now)
            log.info("previsione aggiornata: pioggia %.1f mm/h, raffica %.1f m/s, nuvole %.0f%%",
                     self._weather.rain_rate_mm_h, self._weather.gust_ms, self._weather.cloud)
        except Exception as exc:  # rete assente, DNS, JSON non valido...
            self._next_try = now + RETRY_AFTER_ERROR_S
            log.warning("previsione non disponibile (%s): uso l'ultima valida", exc)

    def _http_fetch(self) -> dict:
        query = urllib.parse.urlencode({
            "latitude": self._lat, "longitude": self._lon,
            "current": CURRENT_VARS, "hourly": HOURLY_VARS,
            "forecast_days": 3, "timeformat": "unixtime",
            "timezone": "GMT", "wind_speed_unit": "ms",
        })
        req = urllib.request.Request(f"{API_URL}?{query}", headers={"User-Agent": "solar-tracker-unoq"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        if payload.get("error"):
            raise RuntimeError(payload.get("reason", "errore Open-Meteo"))
        return payload

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path) as f:
                saved = json.load(f)
            self._weather = parse(saved["payload"], float(saved["fetched_at"]))
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self, payload: dict, now: float) -> None:
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": now, "payload": payload}, f)
            os.replace(tmp, self._cache_path)
        except OSError as exc:
            log.debug("cache meteo non salvata: %s", exc)
