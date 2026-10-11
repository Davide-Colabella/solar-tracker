"""Irrigazione: decide quando aprire l'elettrovalvola e per quanto.

Due modi di capire se la pianta ha sete:
  - con il sensore di umidita' del terreno (quando sara' montato): si innaffia
    sotto la soglia del profilo della pianta;
  - senza sensore: bilancio idrico. Il fabbisogno giornaliero del profilo viene
    scalato con la secchezza dell'aria misurata dall'AHT20 sotto il pannello
    (deficit di pressione di vapore) e si innaffia una dose ogni volta che il
    debito d'acqua accumulato la raggiunge.

Il serbatoio e' a caduta e si riempie con la pioggia: il livello e' stimato,
oppure letto dal galleggiante quando c'e'.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
from controller import (ST_AHT_AMBIENT, ST_AHT_PLANT, ST_SOIL, ST_TANK_OK, ST_TANK_SENSOR,
                        ST_VALVE_OPEN, StateStore, Telemetry)
from plants import PlantProfile
from weather import Weather

log = logging.getLogger("irrigazione")


@dataclass
class WaterRequest:
    seconds: int
    ml: float
    reason: str


def vpd_kpa(temp_c: float, rh_pct: float) -> float:
    """Deficit di pressione di vapore: quanto l'aria 'tira' acqua dalla pianta."""
    saturation = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
    return max(0.0, saturation * (1.0 - rh_pct / 100.0))


class Irrigation:
    def __init__(self, store: StateStore):
        self._store = store
        self.s = store.data
        if self.s["tank_ml"] is None:
            self.s["tank_ml"] = config.TANK_INITIAL_ML
        self._last_ts: float | None = None
        self.blocked: str | None = None     # perche' non si sta innaffiando, per il log
        self.force_reason: str | None = None  # richiesta di innaffiare subito (es. dal controllo salute)

    @property
    def deficit_ml(self) -> float:
        return float(self.s["water_deficit_ml"])

    @property
    def tank_ml(self) -> float:
        return float(self.s["tank_ml"])

    def step(self, now: datetime, sun_elevation: float, tel: Telemetry | None,
             wx: Weather | None, profile: PlantProfile) -> WaterRequest | None:
        """Aggiorna il bilancio e ritorna una richiesta di innaffiatura, se serve ora."""
        ts = now.timestamp()
        dt = 0.0 if self._last_ts is None else max(0.0, min(3600.0, ts - self._last_ts))
        self._last_ts = ts
        s = self.s

        day = (now + timedelta(hours=config.LONGITUDE / 15.0)).strftime("%Y-%m-%d")
        if s["water_day"] != day:
            s["water_day"], s["water_today_ml"] = day, 0.0

        # --- consumo della pianta, scalato con la secchezza dell'aria
        demand = 1.0
        if tel and tel.has(ST_AHT_PLANT):
            demand = vpd_kpa(tel.t_plant, tel.rh_plant) / config.VPD_REFERENCE_KPA
        elif tel and tel.has(ST_AHT_AMBIENT):
            demand = vpd_kpa(tel.t_ambient, tel.rh_ambient) / config.VPD_REFERENCE_KPA
        demand = max(0.3, min(2.5, demand))
        s["water_deficit_ml"] += profile.water_ml_day * demand * dt / 86400.0

        # --- pioggia: riempie il serbatoio e, se arriva alla pianta, la disseta
        raining = bool(wx and wx.raining_now())
        if raining and tel and tel.has(ST_AHT_AMBIENT) and tel.rh_ambient < config.RAIN_CONFIRM_RH:
            raining = False
        if raining:
            rain_mm = wx.rain_rate_mm_h * dt / 3600.0           # 1 mm su 1 m2 = 1 litro
            s["tank_ml"] = min(config.TANK_CAPACITY_ML, s["tank_ml"]
                               + rain_mm * config.COLLECTOR_AREA_M2 * config.COLLECTOR_EFFICIENCY * 1000.0)
            s["water_deficit_ml"] -= rain_mm * config.PLANT_AREA_M2 * config.RAIN_ON_PLANT_FRACTION * 1000.0

        has_soil = bool(tel and tel.has(ST_SOIL))
        if has_soil and tel.soil_pct >= profile.soil_min_pct:
            s["water_deficit_ml"] = 0.0                         # il terreno dice che sta bene
        s["water_deficit_ml"] = max(0.0, min(3.0 * profile.water_ml_day, s["water_deficit_ml"]))

        # --- il galleggiante, se c'e', corregge la stima del serbatoio
        if tel and tel.has(ST_TANK_SENSOR):
            if not tel.has(ST_TANK_OK):
                s["tank_ml"] = 0.0
            elif s["tank_ml"] < config.WATER_DOSE_ML:
                s["tank_ml"] = config.WATER_DOSE_ML

        request = self._decide(ts, sun_elevation, tel, has_soil, raining, profile)
        self._store.save()
        return request

    def _decide(self, ts, sun_elevation, tel, has_soil, raining, profile) -> WaterRequest | None:
        s = self.s
        self.blocked = None
        if not config.IRRIGATION_ENABLED:
            return None
        if has_soil:
            thirsty = tel.soil_pct < profile.soil_min_pct
            urgent = tel.soil_pct < profile.soil_min_pct - 15
            why = f"terreno al {tel.soil_pct:.0f}% (minimo {profile.soil_min_pct}%)"
        else:
            thirsty = s["water_deficit_ml"] >= config.WATER_DOSE_ML
            urgent = s["water_deficit_ml"] >= config.WATER_URGENT_DOSES * config.WATER_DOSE_ML
            why = f"debito stimato {s['water_deficit_ml']:.0f} ml"
        if self.force_reason:
            thirsty = urgent = True
            why = self.force_reason
        if not thirsty:
            return None

        if tel is None:
            self.blocked = "nessuna telemetria: non apro la valvola alla cieca"
        elif tel.has(ST_VALVE_OPEN):
            self.blocked = "valvola gia' aperta"
        elif s["last_water"] is not None and ts - s["last_water"] < config.WATER_MIN_INTERVAL_S:
            self.blocked = "pausa di assorbimento"
        elif s["water_today_ml"] + config.WATER_DOSE_ML > config.WATER_DAILY_CAP_FACTOR * profile.water_ml_day:
            self.blocked = "raggiunto il massimo giornaliero"
        elif s["tank_ml"] < config.WATER_DOSE_ML:
            self.blocked = "serbatoio vuoto"
        elif raining and config.RAIN_ON_PLANT_FRACTION > 0.0:
            self.blocked = "sta piovendo sulla pianta"
        elif sun_elevation > config.WATER_HOT_ELEVATION_DEG and not urgent:
            self.blocked = "ore calde: rimando alla sera"
        if self.blocked:
            return None

        seconds = min(config.WATER_MAX_OPEN_S, max(1, math.ceil(config.WATER_DOSE_ML / config.VALVE_FLOW_ML_S)))
        return WaterRequest(seconds, seconds * config.VALVE_FLOW_ML_S, f"{profile.name}: {why}")

    def confirm(self, ts: float, granted_seconds: int) -> float:
        """Da chiamare quando il microcontrollore ha davvero aperto la valvola. Ritorna i ml erogati."""
        ml = granted_seconds * config.VALVE_FLOW_ML_S
        s = self.s
        s["water_deficit_ml"] = max(0.0, s["water_deficit_ml"] - ml)
        s["tank_ml"] = max(0.0, s["tank_ml"] - ml)
        s["water_today_ml"] += ml
        s["last_water"] = ts
        self.force_reason = None
        self._store.save()
        return ml

    def refund(self, seconds: float) -> float:
        """La valvola e' stata chiusa prima del tempo: toglie dal conto l'acqua non erogata."""
        ml = max(0.0, seconds) * config.VALVE_FLOW_ML_S
        s = self.s
        s["tank_ml"] = min(config.TANK_CAPACITY_ML, s["tank_ml"] + ml)
        s["water_today_ml"] = max(0.0, s["water_today_ml"] - ml)
        s["water_deficit_ml"] += ml
        self._store.save()
        return ml

    def refused(self, ts: float, code: int) -> None:
        """Il microcontrollore ha rifiutato: -1 serbatoio vuoto, -2 pausa minima, -3 gia' aperta."""
        if code == -1:
            self.s["tank_ml"] = 0.0
        self.s["last_water"] = ts          # riprova dopo la pausa, non a ogni ciclo
        self._store.save()
