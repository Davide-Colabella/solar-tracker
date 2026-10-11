"""Logica di decisione: dato sole, meteo, sensori e stato, sceglie la posa.

Non tocca hardware ne' rete, quindi si puo' provare su un PC (vedi tests/).

Priorita', dalla piu' alta:
  1. SICUREZZA   vento forte o vibrazioni -> pannello piatto
  2. NOTTE       pannello a riposo
  3. LUCE_PIANTA il fotoresistore dice che alla pianta arriva poca luce:
                 il pannello si apre (si mette di taglio rispetto al sole)
                 quanto basta, e si richiude quando la luce torna sufficiente
  4. PRODUZIONE  posa che massimizza l'irraggiamento sul pannello
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import plants
import solar
from ai_model import PlantResult
from solar import Pose
from weather import Weather

log = logging.getLogger("controllo")

# Bit di stato della telemetria (devono coincidere con sketch.ino)
ST_AHT_AMBIENT, ST_AHT_PLANT, ST_IMU, ST_INA = 1, 2, 4, 8
ST_SERVO_AZ, ST_SERVO_PITCH, ST_MOVING, ST_FAILSAFE, ST_SYNCED = 16, 32, 64, 128, 256
ST_SOIL, ST_TANK_SENSOR, ST_TANK_OK, ST_VALVE_OPEN, ST_VALVE = 512, 1024, 2048, 4096, 8192
ST_LIGHT = 16384
ST_LIGHT_DIGITAL = 1 << 15      # il fotoresistore dice solo luce/buio (uscita digitale del modulino)
ST_VALVE_LINE1 = 1 << 16        # linea 1 aperta; le linee 2..4 sono i bit successivi

# Stato mostrato dai LED del microcontrollore (come LinuxFlags in sketch.ino)
LX_LIGHT, LX_MANUAL, LX_RAIN, LX_SAFE = 1, 2, 4, 8

SAFE, NIGHT, LIGHT, POWER = "SICUREZZA", "NOTTE", "LUCE_PIANTA", "PRODUZIONE"
MANUAL = "MANUALE"           # posa scelta a mano dalla pagina web


@dataclass
class Telemetry:
    t_ambient: float
    rh_ambient: float
    t_plant: float
    rh_plant: float
    imu_tilt: float
    shake: float
    bus_v: float
    current_ma: float
    az_servo: float
    pitch_servo: float
    soil_pct: float
    status: int
    received_at: float
    light_pct: float = 0.0      # fotoresistore, 0 buio .. 100 piena luce

    def has(self, bit: int) -> bool:
        return bool(self.status & bit)


@dataclass
class Decision:
    mode: str
    pose: Pose
    reason: str
    sun: solar.Sun
    expected_poa: float         # W/m2 attesi sul pannello in questa posa
    dni: float                  # irraggiamento diretto e diffuso usati nel calcolo
    dhi: float
    when: datetime | None = None    # istante (reale o simulato) della decisione
    raining: bool = False
    light_share: float = 0.0        # 0 = puntato al sole, 1 = tutto aperto per dare luce alla pianta


class StateStore:
    """Stato che deve sopravvivere a un riavvio, in un file JSON."""

    DEFAULTS = {
        "plant": None, "plant_confidence": 0.0,
        "light_share": 0.0,
        "az_servo": None, "pitch_servo": None,
        "water_deficit_ml": 0.0, "tank_ml": None, "last_water": None,
        "water_day": None, "water_today_ml": 0.0,
    }

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config.DATA_DIR, "state.json")
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        self._saved = json.dumps(self.data, sort_keys=True)

    def save(self) -> None:
        text = json.dumps(self.data, sort_keys=True)
        if text == self._saved:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, self.path)
            self._saved = text
        except OSError as exc:
            log.debug("stato non salvato: %s", exc)


class Controller:
    def __init__(self, store: StateStore):
        self.s = store.data
        self._store = store
        self._last_mode: str | None = None
        self._last_pose: Pose | None = None
        self._stow_until = 0.0
        # Con risvegli radi conviene puntare dove il sole sara' a meta' intervallo
        self.aim_ahead_s = 0.0
        self.light_step = float(config.LIGHT_STEP)

    # ----------------------------------------------------------------- stato

    @property
    def plant(self) -> plants.PlantProfile:
        return plants.profile_for(self.s["plant"])

    def report_plant(self, result: PlantResult) -> None:
        label = result.best_label(config.PLANT_MIN_CONFIDENCE)
        if label is None:
            return
        confidence = max(result.confidence, result.group_confidence if label == result.group else 0.0)
        if label != self.s["plant"]:
            log.info("pianta riconosciuta: %s (%.0f%%%s)", label, confidence * 100,
                     ", simulato" if result.simulated else "")
        self.s["plant"] = label
        self.s["plant_confidence"] = confidence
        self._store.save()

    # -------------------------------------------------------------- decisione

    def decide(self, now: datetime, tel: Telemetry | None, wx: Weather | None) -> Decision:
        ts = now.timestamp()

        sun = solar.sun_position(now + timedelta(seconds=self.aim_ahead_s), config.LATITUDE, config.LONGITUDE)
        forecast = wx.irradiance(ts) if wx else None
        dni, dhi = forecast if forecast else solar.clear_sky(sun.elevation)
        keep_az = self._last_pose.azimuth if self._last_pose else config.AZ_REF_DEG

        # --- pioggia: previsione, confermata dall'umidita' esterna se c'e' il sensore
        raining = bool(wx and wx.raining_now())
        if raining and tel and tel.has(ST_AHT_AMBIENT) and tel.rh_ambient < config.RAIN_CONFIRM_RH:
            raining = False

        def done(mode: str, pose: Pose, reason: str, poa: float | None = None, share: float = 0.0) -> Decision:
            if poa is None:
                poa = solar.poa_irradiance(sun, dni, dhi, pose.azimuth, pose.tilt)
            self._last_mode, self._last_pose = mode, pose
            self.s["az_servo"], self.s["pitch_servo"] = pose.az_servo, pose.pitch_servo
            self._store.save()
            return Decision(mode, pose, reason, sun, poa, dni, dhi, now, raining, share)

        # --- 1. sicurezza vento
        gust = wx.gust(ts) if wx else 0.0
        shake = tel.shake if tel and tel.has(ST_IMU) and not tel.has(ST_MOVING) else 0.0
        if gust >= config.WIND_STOW_MS or shake >= config.SHAKE_STOW_DPS:
            self._stow_until = ts + config.STOW_HOLD_S
        calm = gust < config.WIND_RELEASE_MS and shake < config.SHAKE_STOW_DPS / 2.0
        if ts < self._stow_until or (self._last_mode == SAFE and not calm):
            return done(SAFE, solar.make_pose(keep_az, config.STOW_TILT_DEG),
                        f"raffiche a {gust:.1f} m/s" + (f", scosse {shake:.0f} gradi/s" if shake > 0 else ""))

        # --- 2. notte
        power_pose, power_poa = solar.best_power_pose(
            sun, dni, dhi, config.RUNOFF_TILT_DEG if raining else None)
        if sun.elevation < config.NIGHT_ELEVATION_DEG or power_poa < 5.0:
            self.s["light_share"] = 0.0
            return done(NIGHT, solar.make_pose(keep_az, config.NIGHT_TILT_DEG), "sole sotto l'orizzonte")

        # --- 3. luce alla pianta: il fotoresistore regola quanto "aprire" il pannello
        profile = self.plant
        light = tel.light_pct if tel and tel.has(ST_LIGHT) else None
        share = float(self.s.get("light_share") or 0.0)
        note = ""
        if not config.PLANT_LIGHT_ENABLED or light is None:
            share = 0.0
        elif tel.has(ST_AHT_PLANT) and tel.t_plant >= profile.max_temp_c:
            share = max(0.0, share - self.light_step)
            note = f"{tel.t_plant:.0f} C sotto il pannello, troppo caldo: richiudo per fare ombra"
        elif light < profile.light_min_pct:
            full = share >= 1.0
            share = min(1.0, share + self.light_step)
            note = (f"luce {light:.0f}% sotto il minimo {profile.light_min_pct}%: "
                    + ("pannello gia' tutto aperto" if full else "apro"))
        elif light > profile.light_min_pct + config.LIGHT_HYSTERESIS_PCT:
            share = max(0.0, share - self.light_step)
            note = f"luce {light:.0f}%, piu' che sufficiente: richiudo"
        else:
            note = f"luce {light:.0f}%, sufficiente: resto cosi'"
        self.s["light_share"] = share
        if share > 0.0:
            open_pose = solar.least_shade_pose(sun)
            pose = solar.make_pose(
                solar.servo_to_az(power_pose.az_servo + share * (open_pose.az_servo - power_pose.az_servo)),
                power_pose.tilt + share * (open_pose.tilt - power_pose.tilt))
            return done(LIGHT, pose, f"{profile.name}: {note} (apertura {share * 100:.0f}%)", share=share)

        # --- 4. massima produzione
        if raining:
            reason = "piove: produzione con il pannello inclinato per far scolare l'acqua"
        elif forecast is None:
            reason = "inseguimento astronomico (nessuna previsione)"
        elif dni < 0.3 * dhi:
            reason = "cielo coperto: quasi piatto per la luce diffusa"
        else:
            reason = "inseguimento del sole"
        return done(POWER, power_pose, reason, power_poa)
