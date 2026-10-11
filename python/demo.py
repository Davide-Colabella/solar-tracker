"""Modalita' dimostrazione: una giornata simulata in pochi minuti.

Si attiva con DEMO_MODE = True in config.py. Il sole e il meteo sono finti e
accelerati; sensori, servo, LED e valvola sono quelli veri. Serve a far vedere
il prototipo al chiuso o di sera, quando nella realta' il pannello starebbe fermo.

Giornata simulata (solstizio d'estate, ore solari approssimate):
    alba        sereno: insegue il sole da est
    mattina     un'ora di cielo coperto: si mette piatto
    mezzogiorno pioggia: resta inclinato per far scolare l'acqua, il serbatoio si riempie
    pomeriggio  raffiche forti: posizione di sicurezza
    sera        sereno: insegue il sole verso ovest, poi ricomincia

Il fotoresistore e' quello vero: coprendolo con una mano il pannello si apre
per dare luce alla pianta, scoprendolo torna a inseguire il sole.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from weather import Hour, Weather

log = logging.getLogger("demo")

DAY_START_H = 4.0     # ore UTC simulate: da poco dopo l'alba...
DAY_END_H = 19.0      # ...a poco dopo il tramonto (a Roma, il 21 giugno)

# (da ora UTC, a ora UTC, descrizione, pioggia mm/h, raffica m/s, vento da gradi, DNI, DHI)
SCENARIO = [
    (4.0, 7.0, "sereno", 0.0, 2.0, 200.0, 750.0, 90.0),
    (7.0, 8.0, "cielo coperto", 0.0, 2.0, 200.0, 0.0, 150.0),
    (8.0, 10.5, "sereno", 0.0, 2.0, 200.0, 800.0, 100.0),
    (10.5, 12.0, "pioggia con vento da sud-ovest", 4.0, 5.0, 220.0, 0.0, 60.0),
    (12.0, 13.5, "sereno", 0.0, 2.0, 200.0, 800.0, 100.0),
    (13.5, 14.5, "raffiche forti", 0.0, 15.0, 270.0, 700.0, 100.0),
    (14.5, 19.0, "sereno", 0.0, 2.0, 200.0, 750.0, 90.0),
]
NIGHT = ("notte", 0.0, 1.0, 200.0, 0.0, 0.0)


def scenario_at(when: datetime):
    """(descrizione, pioggia, raffica, direzione vento, DNI, DHI) all'istante dato."""
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    for start, end, *values in SCENARIO:
        if start <= hour < end:
            return tuple(values)
    return NIGHT


class DemoClock:
    """Orologio simulato: scorre `speed` volte piu' veloce e salta le notti."""

    BASE = datetime(2026, 6, 21, tzinfo=timezone.utc)

    def __init__(self, speed: float, real_clock=time.time):
        self.speed = speed
        self._real = real_clock
        self._t0 = real_clock()

    def _position(self) -> tuple[int, float]:
        day_len = (DAY_END_H - DAY_START_H) * 3600.0
        sim = (self._real() - self._t0) * self.speed
        return int(sim // day_len), sim % day_len

    def now(self) -> datetime:
        day, offset = self._position()
        return self.BASE + timedelta(days=day, hours=DAY_START_H, seconds=offset)

    def day(self) -> int:
        return self._position()[0]


class DemoWeather:
    """Sostituisce il servizio meteo: la 'previsione' e' lo scenario scritto sopra."""

    def get(self, ts: float) -> Weather:
        now = datetime.fromtimestamp(ts, timezone.utc)
        _, rain, gust, wind_dir, _, _ = scenario_at(now)
        w = Weather(fetched_at=ts, rain_rate_mm_h=rain, gust_ms=gust, wind_dir=wind_dir,
                    cloud=100.0 if rain else 0.0)
        first_end = (int(ts // 3600) + 1) * 3600.0
        for i in range(48):
            end = first_end + i * 3600.0
            # ogni ora di previsione prende lo scenario del suo punto centrale;
            # la prima, quella in corso, prende lo scenario di adesso
            ref = now if i == 0 else datetime.fromtimestamp(end - 1800.0, timezone.utc)
            _, h_rain, h_gust, h_dir, dni, dhi = scenario_at(ref)
            w.hours.append(Hour(end=end, precip_mm=h_rain, precip_prob=95.0 if h_rain else 0.0,
                                cloud=100.0 if dni == 0.0 else 0.0, dni=dni, dhi=dhi,
                                gust_ms=h_gust, wind_dir=h_dir))
        return w


class DemoCamera:
    """Nessuna camera in dimostrazione: la pianta si riconosce caricando una foto dalla pagina web."""

    def capture(self):
        return None


class DemoDirector:
    """Tiene il filo della dimostrazione: annuncia i cambi di scena."""

    def __init__(self, clock: DemoClock):
        self.clock = clock
        self._day = -1
        self._scene = None

    def update(self) -> datetime:
        now = self.clock.now()
        if self.clock.day() != self._day:
            self._day = self.clock.day()
            log.info("===== giornata simulata n. %d =====", self._day + 1)
        scene = scenario_at(now)[0]
        if scene != self._scene:
            self._scene = scene
            log.info("scena: %s", scene)
        return now
