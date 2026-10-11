"""Prove della logica su PC, senza scheda: `python tests/test_logic.py` (o pytest).

Il microcontrollore e il meteo sono simulati; il codice sotto prova e' quello
vero della cartella python/.
"""

import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["TRACKER_DATA_DIR"] = tempfile.mkdtemp(prefix="tracker-test-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import config  # noqa: E402
import controller as ctl  # noqa: E402
import solar  # noqa: E402
import weather  # noqa: E402
from ai_model import PlantResult, StubAI  # noqa: E402
from irrigation import Irrigation, vpd_kpa  # noqa: E402
from plants import profile_for  # noqa: E402
from tracker import Tracker  # noqa: E402

UTC = timezone.utc
NOON = datetime(2026, 6, 21, 11, 12, tzinfo=UTC)        # mezzogiorno solare a Roma
ALL_OK = ctl.ST_AHT_AMBIENT | ctl.ST_AHT_PLANT | ctl.ST_IMU | ctl.ST_SERVO_PITCH | ctl.ST_SYNCED
_counter = [0]


def new_store():
    _counter[0] += 1
    return ctl.StateStore(os.path.join(config.DATA_DIR, f"state-{_counter[0]}.json"))


def make_weather(now, rain_mm_h=0.0, gust=2.0, wind_dir=200.0, dni=800.0, dhi=100.0,
                 hourly_rain=None, prob=90.0):
    """Previsione sintetica: 72 ore a partire dall'ora corrente."""
    ts = now.timestamp()
    start = math.floor(ts / 3600.0) * 3600.0
    w = weather.Weather(fetched_at=ts, rain_rate_mm_h=rain_mm_h, gust_ms=gust, wind_dir=wind_dir)
    for i in range(72):
        mm = (hourly_rain or {}).get(i, rain_mm_h if i == 0 else 0.0)
        w.hours.append(weather.Hour(end=start + 3600.0 * (i + 1), precip_mm=mm, precip_prob=prob,
                                    cloud=0.0, dni=dni, dhi=dhi, gust_ms=gust, wind_dir=wind_dir))
    return w


def tel(now, status=ALL_OK, rh_ambient=90.0, t_plant=22.0, shake=0.0, imu_tilt=0.0,
        rh_plant=60.0, soil_pct=0.0, light=None):
    """Senza `light` il fotoresistore risulta assente (come con uno sketch vecchio)."""
    if light is not None:
        status |= ctl.ST_LIGHT
    return ctl.Telemetry(20.0, rh_ambient, t_plant, rh_plant, imu_tilt, shake, 0.0, 0.0, 90.0, 10.0,
                         soil_pct, status, now.timestamp(), light or 0.0)


# ------------------------------------------------------------------ geometria

def test_tracking_points_at_sun():
    c = ctl.Controller(new_store())
    d = c.decide(NOON, tel(NOON), make_weather(NOON))
    assert d.mode == ctl.POWER
    assert abs(d.pose.azimuth - d.sun.azimuth) < 3.0 or d.pose.tilt < 25.0
    assert abs(d.pose.tilt - (90.0 - d.sun.elevation)) < 1.5
    assert solar.cos_incidence(d.sun, d.pose.azimuth, d.pose.tilt) > 0.999


def test_sun_outside_azimuth_range_uses_best_reachable_pose():
    early = datetime(2026, 6, 21, 4, 30, tzinfo=UTC)    # sole a nord-est, fuori corsa
    sun = solar.sun_position(early, config.LATITUDE, config.LONGITUDE)
    assert 0 < sun.elevation < 15 and sun.azimuth < 90
    pose, poa = solar.best_power_pose(sun, 600.0, 0.0)
    assert abs(pose.az_servo - config.AZ_SERVO_MIN) < 1e-6      # fermo a fine corsa est
    # ottimo analitico con azimut bloccato: tan(tilt) = tan(zenit) * cos(delta azimut).
    # Col riflesso del terreno l'ottimo vero si sposta di poco: si confronta l'irraggiamento.
    best = math.degrees(math.atan(math.tan(math.radians(90 - sun.elevation))
                                  * math.cos(math.radians(pose.azimuth - sun.azimuth))))
    assert abs(pose.tilt - min(best, config.TILT_MAX_DEG)) < 2.0
    assert poa >= solar.poa_irradiance(sun, 600.0, 0.0, pose.azimuth, min(best, config.TILT_MAX_DEG)) - 1e-6


def test_overcast_goes_flat_without_spinning_the_base():
    c = ctl.Controller(new_store())
    d = c.decide(NOON, tel(NOON), make_weather(NOON, dni=0.0, dhi=120.0))
    assert d.mode == ctl.POWER and d.pose.tilt == 0.0
    assert abs(d.pose.azimuth - d.sun.azimuth) < 3.0
    assert "coperto" in d.reason


def test_servo_mapping_round_trip_and_limits():
    for az in (90, 135, 180, 225, 270):
        assert abs(solar.servo_to_az(solar.az_to_servo(az)) - az) < 1e-9
    assert solar.az_to_servo(180) == 90 and solar.az_to_servo(90) == 0 and solar.az_to_servo(270) == 180
    p = solar.make_pose(10.0, 200.0)    # nord e oltre la corsa: non raggiungibile
    assert p.az_servo == 0.0 and p.tilt == config.TILT_MAX_DEG
    assert solar.make_pose(350.0, 30.0).az_servo == 180.0


# -------------------------------------------------------------------- pioggia

def test_rain_keeps_producing_with_runoff_tilt():
    c = ctl.Controller(new_store())
    d = c.decide(NOON, tel(NOON), make_weather(NOON, rain_mm_h=4.0, dni=0.0, dhi=60.0))
    assert d.mode == ctl.POWER and d.raining and d.pose.tilt == config.RUNOFF_TILT_DEG


def test_rain_forecast_not_confirmed_by_humidity_sensor():
    c = ctl.Controller(new_store())
    d = c.decide(NOON, tel(NOON, rh_ambient=45.0), make_weather(NOON, rain_mm_h=4.0, hourly_rain={0: 0.0}))
    assert not d.raining
    # senza sensore di umidita' ci si fida della previsione
    d = c.decide(NOON, tel(NOON, status=ALL_OK & ~ctl.ST_AHT_AMBIENT, rh_ambient=0.0),
                 make_weather(NOON, rain_mm_h=4.0))
    assert d.raining


# ------------------------------------------------------------- vento e notte

def test_wind_stow_with_hold_and_hysteresis():
    c = ctl.Controller(new_store())
    now = NOON
    assert c.decide(now, tel(now), make_weather(now)).mode == ctl.POWER
    d = c.decide(now, tel(now), make_weather(now, gust=12.0))
    assert d.mode == ctl.SAFE and d.pose.tilt == config.STOW_TILT_DEG
    now += timedelta(minutes=5)         # vento calato ma ancora dentro il tempo minimo
    assert c.decide(now, tel(now), make_weather(now, gust=3.0)).mode == ctl.SAFE
    now += timedelta(minutes=6)         # tempo scaduto ma raffica sopra la soglia di rilascio
    assert c.decide(now, tel(now), make_weather(now, gust=8.0)).mode == ctl.SAFE
    now += timedelta(minutes=1)
    assert c.decide(now, tel(now), make_weather(now, gust=3.0)).mode == ctl.POWER


def test_imu_shake_triggers_stow_but_not_while_moving():
    c = ctl.Controller(new_store())
    d = c.decide(NOON, tel(NOON, status=ALL_OK | ctl.ST_MOVING, shake=80.0), make_weather(NOON))
    assert d.mode == ctl.POWER
    assert c.decide(NOON, tel(NOON, shake=80.0), make_weather(NOON)).mode == ctl.SAFE


def test_night_parks_flat_and_keeps_azimuth():
    c = ctl.Controller(new_store())
    evening = datetime(2026, 6, 21, 17, 0, tzinfo=UTC)
    az_evening = c.decide(evening, tel(evening), make_weather(evening)).pose.azimuth
    night = datetime(2026, 6, 21, 22, 0, tzinfo=UTC)
    d = c.decide(night, tel(night), make_weather(night, dni=0.0, dhi=0.0))
    assert d.mode == ctl.NIGHT and d.pose.tilt == config.NIGHT_TILT_DEG
    assert abs(d.pose.azimuth - az_evening) < 1e-6


# --------------------------------------------------------------------- pianta

def test_light_sensor_opens_and_closes_the_panel():
    c = ctl.Controller(new_store())
    c.report_plant(PlantResult("Basil", 0.9))
    lo = c.plant.light_min_pct
    hi = lo + config.LIGHT_HYSTERESIS_PCT
    now = [NOON]

    def step(**kw):
        now[0] += timedelta(minutes=1)
        return c.decide(now[0], tel(now[0], **kw), make_weather(now[0]))

    d = step(light=hi + 10)                         # luce abbondante: pensa solo a produrre
    assert d.mode == ctl.POWER and d.light_share == 0.0
    tracking_tilt = d.pose.tilt
    shares = [step(light=lo - 10).light_share for _ in range(6)]    # poca luce: apre un passo alla volta
    assert shares[0] == config.LIGHT_STEP and shares == sorted(shares) and shares[-1] == 1.0
    d = step(light=lo - 10)
    assert d.mode == ctl.LIGHT and "tutto aperto" in d.reason
    assert abs(solar.cos_incidence(d.sun, d.pose.azimuth, d.pose.tilt)) < 0.2    # di taglio: la luce passa
    assert abs(d.pose.tilt - tracking_tilt) > 20                    # e il servo di pitch si vede muovere
    d = step(light=lo + 5)                          # luce giusta: resta com'e'
    assert d.mode == ctl.LIGHT and d.light_share == 1.0
    shares = [step(light=hi + 10).light_share for _ in range(4)]    # luce di nuovo abbondante: richiude
    assert shares[-1] == 0.0
    assert step(light=hi + 10).mode == ctl.POWER

    for _ in range(4):
        step(light=lo - 10)
    d = step(light=lo - 10, t_plant=c.plant.max_temp_c + 3)         # troppo caldo sotto: fa ombra comunque
    assert d.light_share < 1.0 and "caldo" in d.reason
    assert step().mode == ctl.POWER                                 # fotoresistore assente: solo produzione
    d = c.decide(NOON, tel(NOON, light=0.0), make_weather(NOON, gust=20.0))
    assert d.mode == ctl.SAFE                                       # il vento vince sulla luce
    night = datetime(2026, 6, 21, 22, 0, tzinfo=UTC)
    assert c.decide(night, tel(night, light=0.0), make_weather(night, dni=0.0, dhi=0.0)).mode == ctl.NIGHT


def test_light_threshold_follows_the_plant():
    sun_plant, shade_plant = ctl.Controller(new_store()), ctl.Controller(new_store())
    sun_plant.report_plant(PlantResult("pomodoro", 0.9))
    shade_plant.report_plant(PlantResult("spinacio", 0.9))
    light = (sun_plant.plant.light_min_pct + shade_plant.plant.light_min_pct) / 2.0
    assert sun_plant.decide(NOON, tel(NOON, light=light), make_weather(NOON)).mode == ctl.LIGHT
    assert shade_plant.decide(NOON, tel(NOON, light=light), make_weather(NOON)).mode == ctl.POWER


def test_plant_ignored_below_confidence_and_by_default():
    c = ctl.Controller(new_store())
    c.report_plant(PlantResult("pomodoro", 0.3))
    assert c.plant.name == "sconosciuta"
    c.report_plant(PlantResult("pomodoro", 0.9))
    assert c.decide(NOON, tel(NOON), make_weather(NOON)).mode == ctl.POWER


# ---------------------------------------------------------------------- meteo

SAMPLE = {
    "current": {"time": 1000, "interval": 900, "precipitation": 0.5, "cloud_cover": 80,
                "wind_gusts_10m": 6.5, "wind_direction_10m": 210},
    "hourly": {
        "time": [3600, 7200, 10800],
        "precipitation": [0.0, 2.0, None],
        "precipitation_probability": [10, 80, None],
        "cloud_cover": [20, 90, 100],
        "direct_normal_irradiance": [700.0, 50.0, 0.0],
        "diffuse_radiation": [90.0, 150.0, 40.0],
        "wind_gusts_10m": [5.0, 9.0, 11.0],
        "wind_direction_10m": [200, 220, 0],
    },
}


def test_weather_parsing():
    w = weather.parse(SAMPLE, fetched_at=1000.0)
    assert abs(w.rain_rate_mm_h - 2.0) < 1e-9       # 0,5 mm in 15 minuti
    assert w.raining_now() and w.wind_dir == 210.0
    assert w.irradiance(1000.0) == (700.0, 90.0)    # ora che termina a 3600
    assert w.irradiance(3600.0) == (700.0, 90.0) and w.irradiance(3601.0) == (50.0, 150.0)
    assert w.irradiance(20000.0) is None
    assert w.gust(8000.0) == 11.0
    assert w.hours[2].precip_mm == 0.0 and w.hours[2].precip_prob is None and w.hours[2].wind_dir == 0.0
    mm, wait = w.rain_within(1000.0, 3.0)
    assert mm == 2.0 and wait == 2600.0
    assert w.rain_soon(3000.0) and not w.rain_soon(100.0)


def test_weather_service_cache_and_failure():
    calls = []
    online = [True]

    def fetch():
        calls.append(1)
        if not online[0]:
            raise OSError("rete assente")
        return SAMPLE

    svc = weather.WeatherService(41.9, 12.5, fetch=fetch)
    assert svc.get(1000.0) is not None and len(calls) == 1
    assert svc.get(1500.0) is not None and len(calls) == 1          # ancora valida: nessuna richiesta
    online[0] = False
    t = 1000.0 + config.WEATHER_REFRESH_S + 1
    assert svc.get(t) is not None and len(calls) == 2               # errore: resta l'ultima
    assert svc.get(t + 10) is not None and len(calls) == 2          # non martella il server
    assert svc.get(1000.0 + config.WEATHER_MAX_AGE_S + 1000) is None  # troppo vecchia: scartata
    assert weather.WeatherService(41.9, 12.5, fetch=fetch).get(1200.0) is not None  # riletta da disco


# -------------------------------------------------- tracker + MCU simulato

class FakeMcu:
    """Imita le RPC dello sketch (stessi nomi, argomenti interi in centesimi di grado)."""

    def __init__(self):
        self.calls = []
        self.az = self.pitch = None
        self.failsafe = None
        self.synced = False
        self.fail = False
        self.watered_s = 0
        self.water_reply = None
        self.led_tests = []
        self.status = []
        self.boot_stage = 4
        self.boot_queries = 0
        self.sleep_log = []

    def call(self, name, *args, timeout=10):
        if name == "boot_stage":
            self.boot_queries += 1
            if self.boot_stage is None:
                raise ValueError("method boot_stage not available (2)")
            return self.boot_stage
        if self.fail:
            raise TimeoutError("nessuna risposta")
        assert all(type(a) is int for a in args), (name, args)
        self.calls.append((name, args))
        if name == "sync_position":
            self.az, self.pitch = args[0] / 100.0, args[1] / 100.0
            self.synced = True
            return True
        if name == "set_target":
            self.az, self.pitch = args[0] / 100.0, args[1] / 100.0
            self.synced = True
            return 0
        if name == "set_failsafe":
            self.failsafe = (args[0] / 100.0, args[1] / 100.0)
            return True
        if name == "set_low_power":
            self.sleep_log.append(args[0])
            return args[0] > 0
        if name == "led_test":
            self.calls.pop()
            self.led_tests.append(args[0])
            return args[0] > 0
        if name == "set_status":
            self.calls.pop()                    # tenuto a parte: non e' un comando di movimento
            self.status.append(args[0])
            return True
        if name in ("water", "water_manual"):          # (maschera delle linee, secondi per linea)
            mask, seconds = args
            if mask <= 0 or seconds <= 0:
                return 0
            if self.water_reply is not None:
                return self.water_reply
            self.watered_s += seconds * bin(mask).count("1")
            return seconds
        raise ValueError(name)

    def names(self):
        return [c[0] for c in self.calls]

    def moves(self):
        return [c for c in self.calls if c[0] not in ("water", "water_manual")]


class FakeWeather:
    def __init__(self, fn):
        self.fn = fn

    def get(self, ts):
        return self.fn(datetime.fromtimestamp(ts, UTC))


class FakeCamera:
    def capture(self):
        return object()


def make_tracker(mcu, wx_fn):
    return Tracker(mcu, FakeWeather(wx_fn), StubAI(), FakeCamera(), store=new_store())


def feed(tr, mcu, now, imu_tilt=None, **kw):
    status = (ALL_OK & ~ctl.ST_SYNCED) | (ctl.ST_SYNCED if mcu.synced else 0)
    tilt = imu_tilt if imu_tilt is not None else solar.servo_to_tilt(mcu.pitch if mcu.pitch is not None else 10.0)
    t = tel(now, status=status, imu_tilt=abs(tilt), **kw)
    tr.on_telemetry(t.t_ambient, t.rh_ambient, t.t_plant, t.rh_plant, t.imu_tilt, t.shake,
                    t.bus_v, t.current_ma, 90.0, 10.0, t.soil_pct, t.status, t.light_pct)
    tr._telemetry.received_at = now.timestamp()


def test_tracker_startup_sync_heartbeat_and_deadband():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    now = NOON
    feed(tr, mcu, now, imu_tilt=40.0)       # il pannello e' fisicamente a 40 gradi
    tr.tick(now)
    assert mcu.names() == ["sync_position", "set_target", "set_failsafe"]
    assert mcu.calls[0][1] == (9000, int(round(solar.tilt_to_servo(40.0) * 100)))
    first_target = mcu.calls[1][1]
    assert mcu.failsafe == (first_target[0] / 100.0, solar.tilt_to_servo(config.STOW_TILT_DEG))

    mcu.calls.clear()
    now += timedelta(seconds=30)            # prima del periodo di decisione: niente
    feed(tr, mcu, now)
    tr.tick(now)
    assert mcu.calls == []

    now += timedelta(seconds=31)            # il sole si e' mosso meno della banda morta
    feed(tr, mcu, now)
    tr.tick(now)
    assert mcu.calls == [("set_target", first_target)]

    for _ in range(20):                     # dopo 20 minuti il bersaglio cambia
        now += timedelta(seconds=61)
        feed(tr, mcu, now)
        tr.tick(now)
    assert mcu.calls[-1][0] == "set_failsafe" and mcu.calls[-2][1] != first_target


def test_tracker_restart_resumes_from_saved_pose():
    afternoon = datetime(2026, 6, 21, 15, 0, tzinfo=UTC)        # sole a ovest: servo lontano da 90
    store = new_store()
    mcu = FakeMcu()
    tr = Tracker(mcu, FakeWeather(lambda now: make_weather(now)), StubAI(), FakeCamera(), store=store)
    feed(tr, mcu, afternoon)
    tr.tick(afternoon)
    before = mcu.calls[1][1]
    assert abs(before[0] - 9000) > 3000

    # riavvio di Linux e del microcontrollore mezz'ora dopo, senza IMU
    later = afternoon + timedelta(minutes=30)
    mcu2 = FakeMcu()
    tr2 = Tracker(mcu2, FakeWeather(lambda now: make_weather(now)), StubAI(), FakeCamera(),
                  store=ctl.StateStore(store.path))
    t = tel(later, status=ctl.ST_AHT_AMBIENT | ctl.ST_SERVO_PITCH)
    tr2.on_telemetry(t.t_ambient, t.rh_ambient, t.t_plant, t.rh_plant, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, t.status)
    tr2._telemetry.received_at = later.timestamp()
    tr2.tick(later)
    assert mcu2.calls[0] == ("sync_position", before)           # parte da dove era rimasto
    assert mcu2.calls[1][0] == "set_target" and mcu2.calls[1][1] != before


def test_tracker_survives_bridge_errors_and_mcu_reboot():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    now = NOON
    feed(tr, mcu, now)
    tr.tick(now)
    sent = mcu.calls[1][1]

    mcu.fail = True
    now += timedelta(seconds=61)
    feed(tr, mcu, now)
    tr.tick(now)                            # non deve sollevare eccezioni

    mcu.fail = False
    mcu.synced = False                      # il microcontrollore si e' riavviato
    mcu.calls.clear()
    now += timedelta(seconds=61)
    feed(tr, mcu, now)
    tr.tick(now)
    assert mcu.calls[0] == ("sync_position", sent)      # riparte dall'ultima posa nota
    assert mcu.calls[1][0] == "set_target"


def test_tracker_without_telemetry_still_commands_after_timeout():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: None)            # nemmeno il meteo
    now = NOON
    tr.tick(now)
    assert mcu.calls == []                              # aspetta la telemetria
    now += timedelta(seconds=25)
    tr.tick(now)
    assert mcu.names() == ["set_target", "set_failsafe"]
    assert "nessuna previsione" in tr.last_decision.reason


def test_missing_telemetry_is_diagnosed_with_boot_stage():
    import logging
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    logging.getLogger("tracker").addHandler(handler)
    old_level = logging.getLogger("tracker").level
    logging.getLogger("tracker").setLevel(logging.INFO)
    try:
        for stage, expected in ((1, "metodi RPC"), (2, "sensori I2C"), (None, "non risponde")):
            mcu = FakeMcu()
            mcu.boot_stage = stage
            tr = make_tracker(mcu, lambda now: None)
            records.clear()
            now = NOON
            tr.tick(now)
            for _ in range(4):                          # 4 minuti senza telemetria
                now += timedelta(seconds=61)
                tr.tick(now)
            warnings = [m for m in records if "nessuna telemetria" in m]
            assert len(warnings) == 2 and expected in warnings[0], warnings   # un avviso ogni 2 minuti
            assert mcu.boot_queries == 2
        # i messaggi dello sketch finiscono nel log con il prefisso [MCU]
        tr.on_mcu_log("sketch avviato")
        assert records[-1] == "[MCU] sketch avviato"
    finally:
        logging.getLogger("tracker").removeHandler(handler)
        logging.getLogger("tracker").setLevel(old_level)


def test_three_day_story():
    """Tre giorni di fila, con pioggia la mattina del secondo: pose sempre nei limiti, pianta innaffiata."""
    day0 = datetime(2026, 6, 21, 0, 0, tzinfo=UTC)

    def wx(now):
        if (now - day0).days == 1 and 9 <= now.hour < 13:
            return make_weather(now, rain_mm_h=3.0, dni=0.0, dhi=50.0, wind_dir=230.0)
        return make_weather(now)

    mcu = FakeMcu()
    tr = make_tracker(mcu, wx)
    log = []
    now = day0
    while now < day0 + timedelta(days=3):
        feed(tr, mcu, now)
        tr.tick(now)
        d = tr.last_decision
        log.append((now, d.mode, d.raining, d.pose.tilt))
        now += timedelta(seconds=61)

    def at(day, hour):
        target = day0 + timedelta(days=day, hours=hour)
        return min(log, key=lambda r: abs((r[0] - target).total_seconds()))

    assert at(0, 2)[1] == ctl.NIGHT and at(0, 12)[1] == ctl.POWER
    assert at(1, 10)[1:3] == (ctl.POWER, True) and at(1, 10)[3] >= config.RUNOFF_TILT_DEG
    assert at(2, 12)[1:3] == (ctl.POWER, False)
    assert all(r[3] <= config.TILT_MAX_DEG for r in log)
    assert all(0.0 <= mcu_arg <= 18000 for _, args in mcu.moves() for mcu_arg in args)
    # in tre giorni la pianta e' stata innaffiata, ma entro il massimo giornaliero
    profile = tr.controller.plant
    total_ml = mcu.watered_s * config.VALVE_FLOW_ML_S
    assert 1.5 * profile.water_ml_day <= total_ml <= 3 * config.WATER_DAILY_CAP_FACTOR * profile.water_ml_day, total_ml


# ---------------------------------------------------------------- irrigazione

EVENING = datetime(2026, 6, 21, 18, 0, tzinfo=UTC)      # sole basso: si puo' innaffiare
DRY_AIR = dict(t_plant=30.0, rh_plant=40.0)             # deficit di vapore circa 2,5 kPa
MILD_AIR = dict(t_plant=20.0, rh_plant=57.2)            # circa 1,0 kPa


def run_irrigation(irr, start, minutes, profile, wx_fn=lambda now: None, elevation=10.0, auto_confirm=True, **tel_kw):
    """Fa avanzare il bilancio di un minuto alla volta; ritorna [(minuto, richiesta)]."""
    out = []
    now = start
    for m in range(minutes):
        req = irr.step(now, elevation, tel(now, **tel_kw), wx_fn(now), profile)
        if req:
            out.append((m, req))
            if auto_confirm:
                irr.confirm(now.timestamp(), req.seconds)
        now += timedelta(minutes=1)
    return out


def test_vpd_formula():
    assert abs(vpd_kpa(20.0, 57.2) - 1.0) < 0.01
    assert abs(vpd_kpa(30.0, 40.0) - 2.55) < 0.02
    assert vpd_kpa(25.0, 100.0) == 0.0


def test_water_balance_follows_plant_and_air_dryness():
    tomato, spinach = profile_for("pomodoro"), profile_for("spinacio")
    # aria "di riferimento": in 24 ore il debito vale il fabbisogno giornaliero del profilo
    irr = Irrigation(new_store())
    run_irrigation(irr, EVENING, 24 * 60 + 1, tomato, auto_confirm=False, elevation=80.0, **MILD_AIR)
    assert abs(irr.deficit_ml - tomato.water_ml_day) < 0.02 * tomato.water_ml_day
    # aria secca: la stessa pianta chiede acqua prima; una pianta da ombra molto dopo
    first = {}
    for name, prof, air in (("secco", tomato, DRY_AIR), ("mite", tomato, MILD_AIR), ("spinacio", spinach, MILD_AIR)):
        first[name] = run_irrigation(Irrigation(new_store()), EVENING, 24 * 60, prof, **air)[0][0]
    assert first["secco"] < first["mite"] < first["spinacio"]
    assert abs(first["mite"] - 150 / 800 * 1440) <= 2          # 150 ml a 800 ml/giorno = 4,5 ore


def test_dose_is_converted_to_valve_seconds_and_accounted():
    irr = Irrigation(new_store())
    m, req = run_irrigation(irr, EVENING, 600, profile_for("pomodoro"), auto_confirm=False, **MILD_AIR)[0]
    assert req.seconds == 15 and req.ml == 150.0                # 150 ml a 10 ml/s
    tank, deficit = irr.tank_ml, irr.deficit_ml
    assert irr.confirm(EVENING.timestamp(), req.seconds) == 150.0
    assert irr.tank_ml == tank - 150.0 and abs(irr.deficit_ml - (deficit - 150.0)) < 1e-6
    assert irr.s["water_today_ml"] == 150.0


def test_min_interval_and_daily_cap():
    irr = Irrigation(new_store())
    irr.s["water_deficit_ml"] = 2000.0                          # molto assetata
    spinach = profile_for("spinacio")                           # 200 ml/giorno -> massimo 400 ml
    events = run_irrigation(irr, EVENING, 5 * 60, spinach, **MILD_AIR)
    minutes = [m for m, _ in events]
    assert minutes == [0, 30], minutes                          # pausa di 30 minuti, poi stop al massimo
    assert irr.blocked == "raggiunto il massimo giornaliero"
    assert irr.s["water_today_ml"] == 300.0


def test_hot_hours_postpone_unless_urgent():
    irr = Irrigation(new_store())
    irr.s["water_deficit_ml"] = 200.0
    assert irr.step(NOON, 71.0, tel(NOON, **MILD_AIR), None, profile_for("pomodoro")) is None
    assert "sera" in irr.blocked
    assert irr.step(NOON, 20.0, tel(NOON, **MILD_AIR), None, profile_for("pomodoro")) is not None
    irr2 = Irrigation(new_store())
    irr2.s["water_deficit_ml"] = 500.0                          # oltre tre dosi: non si aspetta
    assert irr2.step(NOON, 71.0, tel(NOON, **MILD_AIR), None, profile_for("pomodoro")) is not None


def test_rain_fills_tank_and_empty_tank_blocks():
    irr = Irrigation(new_store())
    irr.s["tank_ml"] = 0.0
    irr.s["water_deficit_ml"] = 400.0
    tomato = profile_for("pomodoro")
    assert irr.step(EVENING, 10.0, tel(EVENING, **MILD_AIR), None, tomato) is None
    assert irr.blocked == "serbatoio vuoto"
    # un'ora di pioggia a 6 mm/h su 0,10 m2 con resa 0,8 = 480 ml
    rain = lambda now: make_weather(now, rain_mm_h=6.0)  # noqa: E731
    events = run_irrigation(irr, EVENING + timedelta(minutes=1), 61, tomato, wx_fn=rain, auto_confirm=False, **MILD_AIR)
    assert abs(irr.tank_ml - 480.0) < 9.0, irr.tank_ml
    assert events and events[0][0] in (18, 19, 20)              # riparte appena c'e' una dose (150 ml)
    # pioggia prevista ma aria secca al sensore: non viene contata
    irr2 = Irrigation(new_store())
    irr2.s["tank_ml"] = 0.0
    run_irrigation(irr2, EVENING, 60, tomato, wx_fn=rain, rh_ambient=40.0, **MILD_AIR)
    assert irr2.tank_ml == 0.0
    # il serbatoio non supera la capienza
    irr3 = Irrigation(new_store())
    run_irrigation(irr3, EVENING, 120, tomato, wx_fn=lambda now: make_weather(now, rain_mm_h=50.0), **MILD_AIR)
    assert irr3.tank_ml <= config.TANK_CAPACITY_ML


def test_rain_reaching_the_plant_reduces_watering():
    old = config.RAIN_ON_PLANT_FRACTION
    config.RAIN_ON_PLANT_FRACTION = 1.0
    try:
        irr = Irrigation(new_store())
        irr.s["water_deficit_ml"] = 400.0
        rain = lambda now: make_weather(now, rain_mm_h=6.0)  # noqa: E731
        events = run_irrigation(irr, EVENING, 60, profile_for("pomodoro"), wx_fn=rain, **MILD_AIR)
        assert events == [] and irr.deficit_ml < 150.0          # 6 mm su 0,05 m2 = 300 ml dal cielo
    finally:
        config.RAIN_ON_PLANT_FRACTION = old


def test_soil_sensor_takes_over_when_present():
    basil = profile_for("basilico")                             # soglia 50%
    with_soil = ALL_OK | ctl.ST_SOIL
    irr = Irrigation(new_store())
    irr.s["water_deficit_ml"] = 400.0
    # terreno umido: niente acqua anche se il bilancio stimato dice il contrario
    assert irr.step(EVENING, 10.0, tel(EVENING, status=with_soil, soil_pct=70.0), None, basil) is None
    assert irr.deficit_ml == 0.0
    # terreno secco: acqua, anche con debito stimato nullo
    req = irr.step(EVENING + timedelta(minutes=1), 10.0, tel(EVENING, status=with_soil, soil_pct=41.0), None, basil)
    assert req is not None and "terreno al 41%" in req.reason


def test_float_switch_overrides_tank_estimate_and_valve_state_blocks():
    tomato = profile_for("pomodoro")
    irr = Irrigation(new_store())
    irr.s["water_deficit_ml"] = 400.0
    empty = ALL_OK | ctl.ST_TANK_SENSOR                         # galleggiante presente, niente acqua
    assert irr.step(EVENING, 10.0, tel(EVENING, status=empty), None, tomato) is None
    assert irr.tank_ml == 0.0 and irr.blocked == "serbatoio vuoto"
    full = ALL_OK | ctl.ST_TANK_SENSOR | ctl.ST_TANK_OK         # ora vede acqua: la stima si corregge
    assert irr.step(EVENING, 10.0, tel(EVENING, status=full), None, tomato) is not None
    assert irr.step(EVENING, 10.0, tel(EVENING, status=full | ctl.ST_VALVE_OPEN), None, tomato) is None
    assert irr.step(EVENING, 10.0, None, None, tomato) is None  # senza telemetria non si apre
    assert "telemetria" in irr.blocked


def test_tracker_waters_through_bridge_and_handles_refusal():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    tr.irrigation.s["water_deficit_ml"] = 400.0
    now = EVENING
    feed(tr, mcu, now)
    tr.tick(now)
    assert ("water", (15, 4)) in mcu.calls and mcu.watered_s == 16    # 15 s di dose: 4 linee da 4 s, in fila
    assert tr.irrigation.s["water_today_ml"] == 160.0 and abs(tr.irrigation.deficit_ml - 240.0) < 1.0
    plan = tr._valve_plan
    assert [round(plan[i][0] - plan[0][0]) for i in range(4)] == [0, 4, 8, 12]

    mcu.water_reply = -1                                        # il microcontrollore dice: serbatoio vuoto
    now += timedelta(minutes=31)
    feed(tr, mcu, now)
    tr.tick(now)
    assert tr.irrigation.tank_ml == 0.0 and tr.irrigation.s["water_today_ml"] == 160.0
    n = len([c for c in mcu.calls if c[0] == "water"])
    now += timedelta(seconds=61)
    feed(tr, mcu, now)
    tr.tick(now)                                                # serbatoio vuoto: non insiste
    assert len([c for c in mcu.calls if c[0] == "water"]) == n


def test_led_status_flags_follow_the_decision():
    wx_box = [lambda now: make_weather(now)]
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: wx_box[0](now))
    now = NOON

    def step(light=80.0):
        nonlocal now
        feed(tr, mcu, now, light=light)
        tr.tick(now)
        now += timedelta(seconds=61)
        return mcu.status[-1]

    assert step() == 0                                          # produzione
    assert step(light=2.0) == ctl.LX_LIGHT                      # poca luce alla pianta: pannello aperto
    tr.set_manual(180.0, 30.0)
    assert step() == ctl.LX_MANUAL                              # comanda la pagina
    tr.set_auto()
    wx_box[0] = lambda now: make_weather(now, rain_mm_h=2.0)
    assert step(light=2.0) == ctl.LX_LIGHT | ctl.LX_RAIN        # piove
    wx_box[0] = lambda now: make_weather(now, gust=14.0, rain_mm_h=2.0)
    assert step() == ctl.LX_RAIN | ctl.LX_SAFE                  # il vento ha la precedenza
    assert all(type(f) is int for f in mcu.status)
    mcu.fail = True                                             # Bridge giu': nessuna eccezione
    step_count = len(mcu.status)
    feed(tr, mcu, now)
    tr.tick(now)
    assert len(mcu.status) == step_count


class FakeImage:
    """Quanto basta a StubAI.assess_health: tre canali con la loro media."""

    def __init__(self, red, green, blue):
        self._means = (red, green, blue)

    def __getitem__(self, key):
        channel = key[-1]

        class _Channel:
            def __init__(self, value):
                self._value = value

            def mean(self):
                return self._value
        return _Channel(self._means[channel])


GREEN_PHOTO = FakeImage(60.0, 140.0, 50.0)
DRY_PHOTO = FakeImage(150.0, 120.0, 60.0)


def test_photo_upload_sets_plant_profile_and_waters_thirsty_plant():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    now = EVENING
    feed(tr, mcu, now)
    tr.tick(now)
    assert tr.controller.plant.name == "sconosciuta" and mcu.watered_s == 0

    result = tr.analyze_photo(GREEN_PHOTO)                      # pianta sana
    assert result["plant"]["label"] == "basilico" and result["plant"]["accepted"]
    assert result["health"]["healthy"] and not result["health"]["needs_water"]
    assert result["profile"]["water_ml_day"] == 350 and tr.controller.plant.name == "basilico"
    assert any("350 ml" in a for a in result["actions"]) and any("sana" in a for a in result["actions"])
    feed(tr, mcu, now)
    tr.tick(now)                                                # la foto anticipa la decisione successiva
    assert mcu.watered_s == 0

    result = tr.analyze_photo(DRY_PHOTO)                        # foglie secche: innaffia subito
    assert result["health"]["needs_water"] and not result["health"]["healthy"]
    assert any("innaffiatura immediata" in a for a in result["actions"])
    feed(tr, mcu, now)
    tr.tick(now)
    assert mcu.watered_s == 16 and tr.irrigation.force_reason is None
    snap = tr.snapshot()
    assert snap["ready"] and snap["photo"] is result and snap["plant"]["name"] == "basilico"
    assert any("Innaffiatura" in e["text"] for e in snap["events"])
    import json
    json.dumps(snap)                                            # la pagina web lo riceve come JSON


def test_model_hub_accepts_simple_results_from_real_models():
    from ai_model import ModelHub

    class Real:
        def classify_plant(self, image):
            return ("Tomato", 0.93)

        def assess_health(self, image):
            return {"label": "Foglie appassite", "confidence": 0.81}

    hub = ModelHub(Real())
    plant, health = hub.classify_plant(None), hub.assess_health(None)
    assert (plant.label, plant.confidence, plant.simulated) == ("Tomato", 0.93, False)
    assert health.needs_water and not health.healthy and not health.simulated   # dedotto dall'etichetta
    healthy = ModelHub(type("M", (), {"assess_health": lambda self, image: ("sana", 0.9)})()).assess_health(None)
    assert healthy.healthy and not healthy.needs_water
    assert ModelHub().classify_plant(object()).simulated          # senza modelli veri: simulato


def test_low_power_wake_cycle():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    tr.low_power = True
    tr.wake_period = tr.decision_period = 1800.0
    tr.controller.aim_ahead_s = 900.0
    slept = []
    tr._sleep = slept.append
    wall = [1000.0]
    tr._clock = lambda: wall[0]
    now = NOON

    def wake():
        # la telemetria fresca arriva mentre il tracker aspetta: la simulo in anticipo
        t = tel(now, status=ALL_OK)
        tr.on_telemetry(t.t_ambient, t.rh_ambient, t.t_plant, t.rh_plant, 0.0, 0.0, 0.0, 0.0, 90.0, 10.0, 0.0, t.status)
        tr._telemetry.received_at = wall[0] + 5.0
        tr.tick(now, wall=wall[0])

    wake()
    assert mcu.sleep_log == [0, 1800]                           # sveglia, lavora, rimette a riposo
    assert mcu.names() == ["set_low_power", "set_target", "set_failsafe", "set_low_power"]
    assert tr.last_decision.mode == ctl.POWER
    assert slept[0] == 4.0 and 1.5 <= slept[-1] <= 10.0         # attesa sensori, poi attesa movimento
    # il sole puntato e' quello di un quarto d'ora dopo
    ahead = solar.sun_position(NOON + timedelta(minutes=15), config.LATITUDE, config.LONGITUDE)
    assert abs(tr.last_decision.sun.azimuth - ahead.azimuth) < 0.01

    calls = len(mcu.calls)
    wall[0] += 600.0                                            # dieci minuti dopo: dorme, nessuna chiamata
    now += timedelta(minutes=10)
    tr.tick(now, wall=wall[0])
    assert len(mcu.calls) == calls and mcu.sleep_log == [0, 1800]

    wall[0] += 1201.0                                           # mezz'ora: nuovo risveglio
    now += timedelta(minutes=20, seconds=1)
    wake()
    assert mcu.sleep_log == [0, 1800, 0, 1800]

    tr.analyze_photo(DRY_PHOTO)                                 # una foto sveglia subito l'impianto
    wall[0] += 5.0
    wake()
    assert mcu.sleep_log[-2:] == [0, 1800] and len(mcu.sleep_log) == 6


def test_manual_control_from_web_page_and_light_sensor():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    wall = [5000.0]
    tr._clock = lambda: wall[0]
    now = NOON

    def step(seconds=1.0, gust=2.0, light=None):
        nonlocal now
        wall[0] += seconds
        now += timedelta(seconds=seconds)
        t = tel(now, light=light)
        tr.on_telemetry(t.t_ambient, t.rh_ambient, t.t_plant, t.rh_plant, 0.0, 0.0, 0.0, 0.0, 90.0, 10.0, 0.0,
                        t.status, *([] if light is None else [light]))
        tr.weather = FakeWeather(lambda n: make_weather(n, gust=gust))
        tr.tick(now, wall=wall[0])

    step()
    assert tr.last_decision.mode == ctl.POWER and tr.snapshot()["light"]["pct"] is None   # 12 argomenti: sketch vecchio
    step(light=73.4)
    light = tr.snapshot()["light"]
    assert light["pct"] == 73 and light["min"] == tr.controller.plant.light_min_pct and light["max"] > light["min"]

    state = tr.set_manual(120.0, 35.0)                          # la pagina prende il comando
    assert state["active"] and (state["azimuth"], state["tilt"]) == (120, 35)
    assert (state["az_min"], state["az_max"], state["tilt_min"], state["tilt_max"]) == (90, 270, 0, config.TILT_MAX_DEG)
    step()                                                      # si muove subito, senza aspettare il minuto
    assert tr.last_decision.mode == ctl.MANUAL
    assert abs(mcu.az - solar.az_to_servo(120.0)) < 0.01 and abs(mcu.pitch - solar.tilt_to_servo(35.0)) < 0.01
    tr.set_manual(120.0, 36.0)                                  # anche un grado solo viene eseguito
    step()
    assert abs(mcu.pitch - solar.tilt_to_servo(36.0)) < 0.01
    assert tr.set_manual(10.0, 200.0)["tilt"] == config.TILT_MAX_DEG   # fuori corsa: limitato

    step(gust=config.WIND_STOW_MS + 5.0)                        # il vento vince sul comando manuale
    assert tr.last_decision.mode == ctl.SAFE and abs(mcu.pitch - solar.tilt_to_servo(config.STOW_TILT_DEG)) < 0.01

    assert not tr.set_auto()["active"]
    tr.set_manual(180.0, 20.0)
    step(config.MANUAL_TIMEOUT_S + 1.0)                         # nessuno tocca piu': torna da solo in automatico
    assert tr.last_decision.mode != ctl.MANUAL and not tr.snapshot()["manual"]["active"]
    assert any("scaduto" in e["text"] for e in tr.events)
    import json
    json.dumps(tr.snapshot())


def test_manual_valve_from_web_page():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    wall = [9000.0]
    tr._clock = lambda: wall[0]
    now = NOON

    def step(seconds=1.0, lines=0):
        nonlocal now
        wall[0] += seconds
        now += timedelta(seconds=seconds)
        feed(tr, mcu, now)
        tr._telemetry.status |= lines * ctl.ST_VALVE_LINE1         # quello che la scheda dice delle linee
        tr._telemetry.received_at = wall[0]
        tr.tick(now, wall=wall[0])

    step()
    decisions = len(mcu.moves())
    tank = tr.irrigation.tank_ml
    assert tr.water_now(500)["seconds"] == config.WATER_MAX_OPEN_S and tr.water_now(5)["lines"] == 15
    tr.water_now(10, lines=0b0101)                              # linee 1 e 3 insieme
    step()                                                      # eseguito subito, senza una nuova decisione
    assert ("water_manual", (5, 10)) in mcu.calls and len(mcu.moves()) == decisions
    assert abs(tank - tr.irrigation.tank_ml - 2 * 10 * config.VALVE_FLOW_ML_S) < 1e-6
    step(1.0, lines=0b0101)
    valve = tr.snapshot()["valve"]
    assert valve["open"] and valve["lines"] == [True, False, True, False] and valve["remaining_s"] == 9
    step(2.0, lines=0b0101)
    tr.water_now(0)                                             # chiuse prima: l'acqua non erogata torna nel conto
    step(1.0)
    assert ("water_manual", (0, 0)) in mcu.calls and not tr.snapshot()["valve"]["open"]
    assert abs(tank - tr.irrigation.tank_ml - 2 * 4 * config.VALVE_FLOW_ML_S) < 1e-6
    assert "linee 1, 3" in " ".join(e["text"] for e in tr.events)

    mcu.water_reply = -1                                        # serbatoio vuoto: lo dice negli avvenimenti
    tr.water_now(5, lines=1)
    step()
    assert "serbatoio vuoto" in tr.events[0]["text"] and not tr.snapshot()["valve"]["open"]
    mcu.water_reply = None
    with tr._lock:                                              # la scheda tace (basso consumo): vale il piano
        tr._telemetry = None
    tr.water_now(5, lines=0b1000)
    wall[0] += 1.0
    tr.tick(now, wall=wall[0])
    assert tr.snapshot()["valve"]["lines"] == [False, False, False, True]
    mcu.fail = True                                             # Bridge giu': nessuna eccezione
    tr.water_now(5)
    step()
    import json
    json.dumps(tr.snapshot())


def test_component_test_mode_from_web_page():
    mcu = FakeMcu()
    tr = make_tracker(mcu, lambda now: make_weather(now))
    tr.low_power = True
    tr._sleep = lambda seconds: None
    wall = [20000.0]
    tr._clock = lambda: wall[0]
    now = NOON

    def step(seconds=1.0):
        nonlocal now
        wall[0] += seconds
        now += timedelta(seconds=seconds)
        feed(tr, mcu, now)
        tr._telemetry.received_at = wall[0]
        tr.tick(now, wall=wall[0])

    step()
    assert mcu.sleep_log[-1] > 0                               # dopo la decisione la scheda dorme
    decided = tr.last_decision
    assert tr.debug_command("servo", axis="pitch", deg=135)["active"]      # un comando avvia la prova
    step()
    assert mcu.sleep_log[-1] == 0                              # scheda svegliata
    assert abs(mcu.pitch - 135.0) < 0.01 and tr.snapshot()["debug"]["active"]
    tr.debug_command("servo", axis="az", deg=999)               # fuori corsa: limitato a 180
    tr.debug_command("leds", mask=8)
    tr.debug_command("stage")
    step()
    assert abs(mcu.az - 180.0) < 0.01 and abs(mcu.pitch - 135.0) < 0.01
    assert mcu.led_tests == [8] and "loop in esecuzione" in tr.snapshot()["debug"]["stage"]
    n = len(mcu.moves())
    for _ in range(25):                                         # in prova non si decide nulla...
        step()
    assert tr.last_decision is decided
    assert len(mcu.moves()) == n + 1                            # ...ma ogni 20 s si ripete la posa: "sono vivo"
    tr.water_now(3)                                             # la valvola si prova anche da qui
    step()
    assert ("water_manual", (15, 3)) in mcu.calls
    sens = tr.snapshot()["debug"]["sensors"]
    assert sens["ambient"]["ok"] and "imu" not in sens and not sens["ina"]["ok"] and sens["age_s"] == 0
    tr.debug_command("stop")
    step()
    assert not tr.snapshot()["debug"]["active"] and tr.last_decision is not decided      # riparte in automatico
    assert mcu.led_tests[-1] == 0 and mcu.sleep_log[-1] > 0 and abs(mcu.pitch - 135.0) > 1.0
    tr.debug_command("start")
    step(config.DEBUG_TIMEOUT_S + 5.0)                          # dimenticata accesa: si chiude da sola
    assert not tr.snapshot()["debug"]["active"] and any("scaduta" in e["text"] for e in tr.events)
    assert tr.debug_command("camera")["ok"] is not None and "error" in tr.debug_command("boh")
    mcu.fail = True                                             # Bridge giu' durante la prova: nessuna eccezione
    tr.debug_command("servo", axis="pitch", deg=10)
    step()
    import json
    json.dumps(tr.snapshot())


def test_repeated_events_are_counted_not_repeated():
    tr = make_tracker(FakeMcu(), lambda now: make_weather(now))
    for _ in range(3):
        tr._event("Ritorno in automatico")
        tr._event("Comando manuale dalla pagina web")
    assert len(tr.events) == 2 and tr.events[0]["count"] == 3 and tr.events[1]["count"] == 3


def test_demo_day_shows_every_behaviour():
    import demo
    real = [1_000_000.0]
    clock = demo.DemoClock(240.0, real_clock=lambda: real[0])
    director = demo.DemoDirector(clock)
    old_rh = config.RAIN_CONFIRM_RH
    config.RAIN_CONFIRM_RH = 0.0
    try:
        mcu = FakeMcu()
        tr = Tracker(mcu, demo.DemoWeather(), StubAI(), demo.DemoCamera(), store=new_store())
        tr.decision_period = 2.0
        seen, tilts, statuses = [], [], set()
        last = None
        assert tr.controller.plant.name == "sconosciuta"         # nessuna camera: serve una foto
        tr.analyze_photo(GREEN_PHOTO)
        for i in range(int(15 * 3600 / 240 / 0.5) + 40):        # una giornata simulata e l'inizio della seconda
            now = director.update()
            covered = 100 <= i < 130                            # una mano copre il fotoresistore per 15 secondi
            assert last is None or now > last                   # l'orologio simulato non torna mai indietro
            last = now
            t = tel(now, rh_ambient=45.0, **DRY_AIR)            # sensori veri: aria secca, niente pioggia
            tr.on_telemetry(t.t_ambient, t.rh_ambient, t.t_plant, t.rh_plant, 0.0, 0.0, 0.0, 0.0, 90.0, 10.0,
                            0.0, ALL_OK | ctl.ST_LIGHT, 3.0 if covered else 80.0)
            tr._telemetry.received_at = real[0]
            tr.tick(now=now, wall=real[0])
            if tr.last_decision:
                d = tr.last_decision
                if not seen or seen[-1] != (d.mode, d.reason.split(":")[0]):
                    seen.append((d.mode, d.reason.split(":")[0]))
                tilts.append(d.pose.tilt)
            if mcu.status:
                statuses.add(mcu.status[-1])
            real[0] += 0.5
        modes = [m for m, _ in seen]
        reasons = " | ".join(r for _, r in seen)
        assert ctl.POWER in modes and ctl.LIGHT in modes and ctl.SAFE in modes, seen
        assert "inseguimento del sole" in reasons and "cielo coperto" in reasons, reasons
        assert tr.controller.plant.name == "basilico"
        assert max(tilts) - min(tilts) > 50                     # il servo ha molto da fare
        assert mcu.watered_s >= 15                              # almeno un'innaffiatura
        assert {0, ctl.LX_LIGHT, ctl.LX_RAIN, ctl.LX_SAFE} <= statuses, statuses
        assert clock.day() == 1                                 # la seconda giornata e' cominciata
    finally:
        config.RAIN_CONFIRM_RH = old_rh


def test_irrigation_can_be_disabled():
    old = config.IRRIGATION_ENABLED
    config.IRRIGATION_ENABLED = False
    try:
        irr = Irrigation(new_store())
        irr.s["water_deficit_ml"] = 900.0
        assert irr.step(EVENING, 10.0, tel(EVENING), None, profile_for("pomodoro")) is None
    finally:
        config.IRRIGATION_ENABLED = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} prove superate")
    sys.exit(1 if failed else 0)
