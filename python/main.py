"""Inseguitore solare intelligente - lato Linux della Arduino UNO Q.

Legge la telemetria del microcontrollore (sensori, fotoresistore), scarica le
previsioni meteo, usa i modelli AI (tipo e salute della pianta), comanda i due
servo e l'irrigazione, e pubblica la pagina web.
"""

import logging
import os
import time
from datetime import timedelta

from arduino.app_utils import App, Bridge

import config
import webui
from ai_model import load_model
from controller import StateStore
from devices import PlantCamera
from tracker import Tracker
from weather import WeatherService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
log = logging.getLogger("main")

director = None
if config.DEMO_MODE:
    import demo

    log.warning("MODALITA' DIMOSTRAZIONE: sole e meteo sono simulati e accelerati "
                "(DEMO_MODE = False in config.py per l'uso reale)")
    config.RAIN_CONFIRM_RH = 0.0            # la pioggia finta non bagna il sensore vero
    demo_state = os.path.join(config.DATA_DIR, "state-demo.json")
    try:
        os.remove(demo_state)               # ogni dimostrazione riparte da zero
    except OSError:
        pass
    director = demo.DemoDirector(demo.DemoClock(config.DEMO_SPEED))
    tracker = Tracker(bridge=Bridge, weather=demo.DemoWeather(), model=load_model(),
                      plant_camera=demo.DemoCamera(), store=StateStore(demo_state))
    tracker.demo = True
    tracker.decision_period = config.DEMO_STEP_S
    tracker.controller.light_step = 0.5         # reazione alla luce ben visibile: tutta la corsa in due passi
    tracker.status_period = 20.0
    # ora simulata, mostrata come ora italiana (UTC+2)
    tracker.time_label = lambda now: f"[DEMO {now + timedelta(hours=2):%H:%M}] "
else:
    tracker = Tracker(
        bridge=Bridge,
        weather=WeatherService(config.LATITUDE, config.LONGITUDE),
        model=load_model(),
        plant_camera=PlantCamera(config.PLANT_CAMERA_SOURCE),
    )
    if config.LOW_POWER_MODE:
        log.info("basso consumo attivo: un risveglio ogni %.0f minuti", config.WAKE_PERIOD_S / 60.0)
        tracker.low_power = True
        tracker.wake_period = float(config.WAKE_PERIOD_S)
        tracker.decision_period = float(config.WAKE_PERIOD_S)
        tracker.controller.aim_ahead_s = config.WAKE_PERIOD_S / 2.0
        tracker.controller.light_step = 0.5     # pochi risvegli: passi piu' grandi verso la luce giusta

if config.DEMO_MODE:                        # in dimostrazione la camera serve solo alla prova dei componenti
    tracker.test_camera = PlantCamera(config.PLANT_CAMERA_SOURCE)

# Pagina web: stato in tempo reale, foto per i due modelli, comando manuale del pannello
ui = webui.start(tracker)

# Lo sketch chiama Bridge.notify("telemetry", ...) ogni 2 secondi
Bridge.provide("telemetry", tracker.on_telemetry)
# ...e Bridge.notify("mcu_log", testo) per i suoi messaggi di stato
Bridge.provide("mcu_log", tracker.on_mcu_log)


def loop():
    try:
        if director is not None:
            time.sleep(0.5)
            tracker.tick(now=director.update(), wall=time.time())
        else:
            time.sleep(1)
            tracker.tick()
    except Exception:
        log.exception("errore nel ciclo di controllo")


App.run(user_loop=loop)
