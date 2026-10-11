"""Posizione del sole e ricerca dell'orientamento migliore del pannello."""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass
class Sun:
    elevation: float  # gradi sopra l'orizzonte (con rifrazione)
    azimuth: float    # gradi da nord in senso orario


@dataclass
class Pose:
    azimuth: float       # direzione verso cui guarda il pannello (bussola)
    tilt: float          # 0 = piatto
    az_servo: float
    pitch_servo: float


def sun_position(when: datetime, lat: float, lon: float) -> Sun:
    """Algoritmo NOAA (Meeus semplificato), errore tipico < 0,1 gradi."""
    when = when.astimezone(timezone.utc)
    jd = when.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))

    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app_long))

    y = math.tan(eps / 2.0) ** 2
    l0r = math.radians(l0)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))

    minutes = when.hour * 60.0 + when.minute + when.second / 60.0
    true_solar = (minutes + eq_time + 4.0 * lon) % 1440.0
    hour_angle = math.radians(true_solar / 4.0 - 180.0)

    latr = math.radians(lat)
    cos_zen = (math.sin(latr) * math.sin(decl)
               + math.cos(latr) * math.cos(decl) * math.cos(hour_angle))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zen = math.acos(cos_zen)
    elevation = 90.0 - math.degrees(zen)

    azimuth = (math.degrees(math.atan2(
        math.sin(hour_angle),
        math.cos(hour_angle) * math.sin(latr) - math.tan(decl) * math.cos(latr))) + 180.0) % 360.0

    # rifrazione atmosferica
    if elevation > 85.0:
        refr = 0.0
    elif elevation > 5.0:
        te = math.tan(math.radians(elevation))
        refr = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
    elif elevation > -0.575:
        refr = 1735.0 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)))
    else:
        refr = -20.772 / math.tan(math.radians(elevation))
    return Sun(elevation + refr / 3600.0, azimuth)


def clear_sky(elevation: float) -> tuple[float, float]:
    """Stima (DNI, DHI) in W/m2 a cielo sereno, usata se manca la previsione."""
    if elevation <= 0.0:
        return 0.0, 0.0
    air_mass = 1.0 / (math.sin(math.radians(elevation)) + 0.50572 * (elevation + 6.07995) ** -1.6364)
    dni = 1353.0 * 0.7 ** (air_mass ** 0.678)
    dhi = 0.1 * dni * math.sin(math.radians(elevation))
    return dni, dhi


def _vec(azimuth: float, tilt_from_vertical: float) -> tuple[float, float, float]:
    """Versore (nord, est, alto) dati azimut e angolo dalla verticale."""
    a, z = math.radians(azimuth), math.radians(tilt_from_vertical)
    return math.sin(z) * math.cos(a), math.sin(z) * math.sin(a), math.cos(z)


def cos_incidence(sun: Sun, azimuth: float, tilt: float) -> float:
    """Coseno dell'angolo tra il sole e la normale al pannello."""
    s = _vec(sun.azimuth, 90.0 - sun.elevation)
    n = _vec(azimuth, tilt)
    return s[0] * n[0] + s[1] * n[1] + s[2] * n[2]


def poa_irradiance(sun: Sun, dni: float, dhi: float, azimuth: float, tilt: float) -> float:
    """Irraggiamento sul piano del pannello (modello isotropo), W/m2."""
    ct = math.cos(math.radians(tilt))
    direct = dni * max(0.0, cos_incidence(sun, azimuth, tilt)) if sun.elevation > 0 else 0.0
    ghi = dni * max(0.0, math.sin(math.radians(sun.elevation))) + dhi
    return direct + dhi * (1.0 + ct) / 2.0 + ghi * config.ALBEDO * (1.0 - ct) / 2.0


# ------------------------------------------------------------------ cinematica

def az_to_servo(azimuth: float) -> float:
    delta = (azimuth - config.AZ_REF_DEG + 180.0) % 360.0 - 180.0
    return config.AZ_SERVO_AT_REF + config.AZ_SERVO_DIR * delta


def servo_to_az(servo: float) -> float:
    return (config.AZ_REF_DEG + (servo - config.AZ_SERVO_AT_REF) / config.AZ_SERVO_DIR) % 360.0


def tilt_to_servo(tilt: float) -> float:
    return config.PITCH_SERVO_AT_FLAT + config.PITCH_SERVO_DIR * tilt


def servo_to_tilt(servo: float) -> float:
    return (servo - config.PITCH_SERVO_AT_FLAT) / config.PITCH_SERVO_DIR


def make_pose(azimuth: float, tilt: float) -> Pose:
    """Porta una posa richiesta dentro i limiti meccanici."""
    az_servo = min(config.AZ_SERVO_MAX, max(config.AZ_SERVO_MIN, az_to_servo(azimuth)))
    tilt = min(config.TILT_MAX_DEG, max(config.TILT_MIN_DEG, tilt))
    return Pose(servo_to_az(az_servo), tilt, az_servo, tilt_to_servo(tilt))


def _az_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _search(score, tilt_min: float, tilt_max: float, prefer_az: float) -> Pose:
    """Massimizza score(azimut, tilt) sulle pose raggiungibili: griglia + raffinamento.

    A parita' di punteggio (es. cielo coperto, dove l'azimut non conta) vince
    la posa piu' vicina a `prefer_az`, per non ruotare la base inutilmente.
    """
    best = (-math.inf, config.AZ_SERVO_AT_REF, tilt_min)

    def scan(servo_lo, servo_hi, tilt_lo, tilt_hi, step):
        nonlocal best
        n_s = int(round((servo_hi - servo_lo) / step))
        n_t = int(round((tilt_hi - tilt_lo) / step))
        for i in range(n_s + 1):
            sv = min(servo_hi, servo_lo + i * step)
            az = servo_to_az(sv)
            for j in range(n_t + 1):
                tl = min(tilt_hi, tilt_lo + j * step)
                val = score(az, tl) - 1e-4 * _az_diff(az, prefer_az)
                if val > best[0] + 1e-9:
                    best = (val, sv, tl)

    scan(config.AZ_SERVO_MIN, config.AZ_SERVO_MAX, tilt_min, tilt_max, 3.0)
    _, sv, tl = best
    scan(max(config.AZ_SERVO_MIN, sv - 3.0), min(config.AZ_SERVO_MAX, sv + 3.0),
         max(tilt_min, tl - 3.0), min(tilt_max, tl + 3.0), 0.5)
    _, sv, tl = best
    return Pose(servo_to_az(sv), tl, sv, tilt_to_servo(tl))


def best_power_pose(sun: Sun, dni: float, dhi: float, min_tilt: float | None = None) -> tuple[Pose, float]:
    """Posa raggiungibile che massimizza l'irraggiamento sul pannello.

    Con cielo sereno coincide con l'inseguimento del sole; con cielo coperto
    (solo diffusa) tende al pannello piatto. Ritorna (posa, W/m2 attesi).
    """
    lo = config.TILT_MIN_DEG if min_tilt is None else max(config.TILT_MIN_DEG, min_tilt)
    lo = min(lo, config.TILT_MAX_DEG)
    pose = _search(lambda az, tl: poa_irradiance(sun, dni, dhi, az, tl), lo, config.TILT_MAX_DEG,
                   prefer_az=sun.azimuth)
    return pose, poa_irradiance(sun, dni, dhi, pose.azimuth, pose.tilt)


def least_shade_pose(sun: Sun) -> Pose:
    """Pannello di taglio rispetto al sole: la pianta sotto riceve luce diretta.

    A parita' di taglio preferisce la posa piu' ripida: un pannello in piedi
    copre meno cielo sopra la pianta.
    """
    return _search(lambda az, tl: -abs(cos_incidence(sun, az, tl)) + 1e-3 * abs(tl),
                   config.TILT_MIN_DEG, config.TILT_MAX_DEG, prefer_az=sun.azimuth)
