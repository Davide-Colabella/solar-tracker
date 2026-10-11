"""Orchestrazione: collega telemetria, meteo, modello AI, decisione e servo.

Il Bridge viene passato dall'esterno, cosi' questo modulo gira anche su un PC
con un microcontrollore finto (vedi tests/).
"""

import dataclasses
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import config
import solar
from controller import (LIGHT, LX_LIGHT, LX_MANUAL, LX_RAIN, LX_SAFE, MANUAL, SAFE, ST_AHT_AMBIENT, ST_AHT_PLANT,
                        ST_FAILSAFE, ST_IMU, ST_INA, ST_LIGHT, ST_MOVING, ST_SERVO_AZ, ST_SERVO_PITCH, ST_SOIL,
                        ST_LIGHT_DIGITAL, ST_SYNCED, ST_TANK_OK, ST_TANK_SENSOR, ST_VALVE, ST_VALVE_LINE1,
                        ST_VALVE_OPEN, Controller, Decision, StateStore, Telemetry)
from irrigation import Irrigation
from solar import Pose

log = logging.getLogger("tracker")


# Punti di avanzamento dello sketch (come BootStage in sketch.ino)
BOOT_STAGES = {
    0: "prima di Bridge.begin()",
    1: "registrazione dei metodi RPC",
    2: "inizializzazione dei sensori I2C",
    3: "passo non piu' usato",
    4: "loop in esecuzione",
    5: "passo non piu' usato",
}


def _cdeg(deg: float) -> int:
    """Gli angoli viaggiano sul Bridge come interi in centesimi di grado."""
    return int(round(deg * 100.0))


class Tracker:
    def __init__(self, bridge, weather, model, plant_camera, store: StateStore | None = None):
        self.bridge = bridge
        self.weather = weather
        self.model = model
        self.plant_camera = plant_camera
        self.store = store or StateStore()
        # Ultima posa nota prima di questo avvio (la decisione successiva la sovrascrive)
        self._boot_az = self.store.data["az_servo"]
        self._boot_pitch = self.store.data["pitch_servo"]
        self.controller = Controller(self.store)
        self.irrigation = Irrigation(self.store)

        self._lock = threading.Lock()
        self._telemetry: Telemetry | None = None
        self._started_at: float | None = None
        self._next_decision = 0.0
        self._next_plant = 0.0
        self._sent: Pose | None = None
        self._sent_mode: str | None = None
        self._sent_at = 0.0
        self._warned: dict[str, float] = {}
        self.last_decision: Decision | None = None
        self._wall = 0.0                    # orologio reale dell'ultimo tick
        self._last_status = -1e12
        self.decision_period = float(config.DECISION_PERIOD_S)
        self.status_period = 0.0            # secondi reali tra due righe di stato (0 = a ogni decisione)
        self.time_label = lambda now: ""    # prefisso dei messaggi (usato dalla dimostrazione)

        # Basso consumo: una decisione ogni wake_period, poi il microcontrollore "dorme"
        self.low_power = False
        self.wake_period = float(config.WAKE_PERIOD_S)
        self._sleep = time.sleep
        self._clock = time.time

        # Per l'interfaccia web
        self.demo = False
        self.events: deque = deque(maxlen=40)       # ultimi avvenimenti, dal piu' recente
        self.photo: dict | None = None              # esito dell'ultima foto analizzata
        self._snapshot: dict = {"ready": False}
        self._last_now: datetime | None = None
        self._manual: dict | None = None            # {"azimuth", "tilt", "until"} mentre comanda la pagina
        self._kick = False                          # la pagina chiede una decisione immediata
        self._valve_cmd: tuple | None = None        # comando a mano delle valvole: (maschera delle linee, secondi)
        self._valve_plan: dict = {}                 # linea -> (apre, chiude), orologio reale
        # Prova dei componenti (pagina "Prova componenti"): finche' e' attiva non si decide nulla
        self._debug: dict | None = None
        self._debug_stage: str | None = None        # ultima risposta della scheda a "a che punto sei?"
        self.mcu_log: deque = deque(maxlen=12)      # ultimi messaggi di testo dello sketch
        self.test_camera = None                     # camera da provare (di solito la stessa della pianta)
        self._last_wx = None
        # Taratura del fotoresistore: lettura grezza al buio e in piena luce (resta anche in dimostrazione)
        self._light_file = os.path.join(config.DATA_DIR, "light-calibration.json")
        self._light_cal = {"dark": None, "bright": None}
        self._light_raw: float | None = None
        try:
            with open(self._light_file) as f:
                self._light_cal.update({k: v for k, v in json.load(f).items() if k in self._light_cal})
        except (OSError, ValueError):
            pass

    # -------------------------------------------------------------- telemetria

    def on_telemetry(self, t_amb, rh_amb, t_plant, rh_plant, imu_tilt, shake,
                     bus_v, current_ma, az_servo, pitch_servo, soil_pct, status, light_pct=0.0) -> None:
        """Chiamata dal Bridge ogni 2 s con i dati del microcontrollore."""
        self._light_raw = float(light_pct) if int(status) & ST_LIGHT else None
        tel = Telemetry(float(t_amb), float(rh_amb), float(t_plant), float(rh_plant),
                        float(imu_tilt), float(shake), float(bus_v), float(current_ma),
                        float(az_servo), float(pitch_servo), float(soil_pct), int(status), self._clock(),
                        self._scale_light(float(light_pct), bool(int(status) & ST_LIGHT_DIGITAL)))
        with self._lock:
            self._telemetry = tel

    def _scale_light(self, raw: float, digital: bool = False) -> float:
        """Porta la lettura grezza sulla scala 0 (buio tarato) .. 100 (luce tarata)."""
        if digital:                         # luce/buio dal modulino: e' gia' 0 oppure 100, la taratura non serve
            return raw
        dark, bright = self._light_cal["dark"], self._light_cal["bright"]
        if dark is None or bright is None or abs(bright - dark) < config.LIGHT_CAL_MIN_SPAN:
            return raw
        return max(0.0, min(100.0, (raw - dark) * 100.0 / (bright - dark)))

    def calibrate_light(self, point: str) -> dict:
        """Dalla pagina web: la lettura di adesso e' il "buio" o la "piena luce"; "reset" azzera."""
        if point == "reset":
            self._light_cal = {"dark": None, "bright": None}
        elif point in ("dark", "bright"):
            if self._light_raw is None:
                return {"error": "Il fotoresistore non sta mandando letture."}
            self._light_cal[point] = round(self._light_raw, 1)
        else:
            return {"error": "Punto di taratura non valido."}
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(self._light_file, "w") as f:
                json.dump(self._light_cal, f)
        except OSError as exc:
            log.warning("taratura della luce non salvata: %s", exc)
        log.info("taratura fotoresistore: buio %s, piena luce %s", self._light_cal["dark"], self._light_cal["bright"])
        self._event({"dark": "Fotoresistore: tarato il buio", "bright": "Fotoresistore: tarata la piena luce",
                     "reset": "Fotoresistore: taratura azzerata"}[point])
        return self._light_state()

    def _light_state(self) -> dict:
        dark, bright = self._light_cal["dark"], self._light_cal["bright"]
        active = dark is not None and bright is not None and abs(bright - dark) >= config.LIGHT_CAL_MIN_SPAN
        too_close = dark is not None and bright is not None and not active
        return {"raw": None if self._light_raw is None else round(self._light_raw),
                "dark": dark, "bright": bright, "calibrated": active, "too_close": too_close}

    def on_mcu_log(self, text) -> None:
        """Messaggi di testo dello sketch (Bridge.notify("mcu_log", ...))."""
        log.info("[MCU] %s", text)
        self.mcu_log.appendleft({"time": time.strftime("%H:%M:%S"), "text": str(text)})

    def _diagnose_mcu(self, ts: float) -> None:
        """Senza telemetria, chiede allo sketch a che punto e' arrivato."""
        if self._wall - self._warned.get("telemetria", -1e12) < 120.0:
            return
        self._warned["telemetria"] = self._wall
        try:
            stage = int(self.bridge.call("boot_stage", timeout=3))
        except Exception as exc:
            log.warning("nessuna telemetria dal microcontrollore e lo sketch non risponde (%s): "
                        "non e' partito, oppure e' fermo in Bridge.begin()", exc)
            return
        log.warning("nessuna telemetria dal microcontrollore: lo sketch e' fermo a \"%s\"",
                    BOOT_STAGES.get(stage, f"passo {stage}"))

    def telemetry(self, now_ts: float) -> Telemetry | None:
        with self._lock:
            tel = self._telemetry
        if tel is None or now_ts - tel.received_at > config.TELEMETRY_STALE_S:
            return None
        return tel

    # -------------------------------------------------------------------- ciclo

    def tick(self, now: datetime | None = None, wall: float | None = None) -> None:
        """Da chiamare circa una volta al secondo.

        `now` e' l'ora usata per sole, meteo e logica (puo' essere simulata);
        `wall` e' l'orologio reale, che regola cadenza e freschezza della telemetria.
        """
        now = now or datetime.now(timezone.utc)
        ts = now.timestamp()
        wall = ts if wall is None else wall
        self._wall = wall
        self._last_now = now
        if self._started_at is None:
            self._started_at = wall
            self._next_plant = ts + 60.0
        manual = self._manual
        if manual is not None and wall >= manual["until"]:
            self._manual = None
            self._kick = True
            self._event("Comando manuale scaduto: ritorno in automatico")
        cmd, self._valve_cmd = self._valve_cmd, None
        if cmd is not None:
            self._manual_valve(cmd[0], cmd[1], ts)  # subito, senza aspettare la prossima decisione
        debug = self._debug
        if debug is not None:
            if wall >= debug["until"]:
                self._debug_end("Prova dei componenti scaduta: ritorno in automatico")
            else:
                self._debug_tick(debug, wall, ts)
                return                              # in prova l'automatismo resta fermo
        if self._kick:
            self._kick = False
            self._next_decision = 0.0
        if wall < self._next_decision:
            return
        if self.low_power:
            tel = self._wake_mcu()
        else:
            tel = self.telemetry(wall)
            if tel is None and wall - self._started_at < 20.0:
                return                  # all'avvio aspetta la prima telemetria
        self._next_decision = wall + self.decision_period
        previous = self._sent

        if tel is None:
            self._diagnose_mcu(ts)

        wx = self.weather.get(ts)
        self._last_wx = wx
        decision = self.controller.decide(now, tel, wx)
        manual = self._manual
        if manual is not None and decision.mode != SAFE:    # vento e scosse vincono sul comando manuale
            pose = solar.make_pose(manual["azimuth"], manual["tilt"])
            decision = dataclasses.replace(
                decision, mode=MANUAL, pose=pose, reason="posa scelta dalla pagina web", light_share=0.0,
                expected_poa=solar.poa_irradiance(decision.sun, decision.dni, decision.dhi, pose.azimuth, pose.tilt))
        if self.last_decision is None or decision.mode != self.last_decision.mode:
            self._event(f"Modalità {decision.mode}: {decision.reason}")
        self.last_decision = decision
        self._apply(decision, tel, ts)

        self._check_plant(decision, ts)
        self._check_pitch(decision, tel, ts)
        self._check_irrigation(decision, tel, wx, now)
        self._log_status(decision, tel, ts)

        self._snapshot = self._build_snapshot(decision, tel, wx, now)

        if self.low_power:
            self._back_to_sleep(previous)

    # ----------------------------------------------------------- basso consumo

    def _wake_mcu(self) -> Telemetry | None:
        """Sveglia il microcontrollore e aspetta una lettura fresca dei sensori."""
        mark = self._clock()
        try:
            self.bridge.call("set_low_power", 0)
        except Exception as exc:
            self._warn("risveglio", 0.0, f"risveglio del microcontrollore non riuscito: {exc}")
        self._sleep(4.0)                    # il tempo di una misura dei sensori
        for _ in range(32):                 # poi fino ad altri 8 secondi
            with self._lock:
                tel = self._telemetry
            if tel is not None and tel.received_at >= mark + 3.0:
                return tel
            self._sleep(0.25)
        return None

    def _back_to_sleep(self, previous: Pose | None) -> None:
        """Aspetta la fine del movimento e rimette a riposo il microcontrollore."""
        target = self._sent
        travel = 180.0
        if previous is not None and target is not None:
            travel = max(abs(target.az_servo - previous.az_servo), abs(target.pitch_servo - previous.pitch_servo))
        self._sleep(min(10.0, travel / config.SERVO_SPEED_DEG_S + 1.5))
        try:
            self.bridge.call("set_low_power", int(self.wake_period))
            log.info("basso consumo: prossimo risveglio tra %.0f minuti", self.wake_period / 60.0)
        except Exception as exc:
            self._warn("riposo", 0.0, f"messa a riposo del microcontrollore non riuscita: {exc}")

    # ------------------------------------------------------------------- servo

    def _apply(self, decision: Decision, tel: Telemetry | None, ts: float) -> None:
        pose = decision.pose
        moved_enough = (
            self._sent is None
            or decision.mode != self._sent_mode
            or (decision.mode == MANUAL and (pose.az_servo != self._sent.az_servo
                                             or pose.pitch_servo != self._sent.pitch_servo))
            or abs(pose.az_servo - self._sent.az_servo) >= config.MOVE_DEADBAND_DEG
            or abs(pose.pitch_servo - self._sent.pitch_servo) >= config.MOVE_DEADBAND_DEG)
        target = pose if moved_enough else self._sent
        try:
            if tel is not None and not tel.has(ST_SYNCED):
                self._sync(tel)
            # Rimandato a ogni ciclo: fa anche da "sono vivo" per il microcontrollore
            clamped = self.bridge.call("set_target", _cdeg(target.az_servo), _cdeg(target.pitch_servo))
            if clamped:
                self._warn("limiti", ts, "il microcontrollore ha limitato la posa richiesta")
            if moved_enough:
                self.bridge.call("set_failsafe", _cdeg(target.az_servo),
                                 _cdeg(solar.tilt_to_servo(config.STOW_TILT_DEG)))
                self._sent, self._sent_mode, self._sent_at = target, decision.mode, ts
                log.info("%s%s -> azimut %.0f, tilt %.0f (%s)", self.time_label(decision.when),
                         decision.mode, target.azimuth, target.tilt, decision.reason)
        except Exception as exc:
            self._warn("bridge", ts, f"comando ai servo non riuscito: {exc}")
        self._send_status(decision, ts)

    def _send_status(self, decision: Decision, ts: float) -> None:
        """Stato per i LED del microcontrollore."""
        flags = ((LX_LIGHT if decision.mode == LIGHT else 0)
                 | (LX_MANUAL if decision.mode == MANUAL else 0)
                 | (LX_RAIN if decision.raining else 0)
                 | (LX_SAFE if decision.mode == SAFE else 0))
        try:
            self.bridge.call("set_status", int(flags))
        except Exception as exc:
            self._warn("led", ts, f"stato dei LED non inviato: {exc}")

    def _sync(self, tel: Telemetry) -> None:
        """Dice al microcontrollore dove si trovano gli assi, per partire senza strappi."""
        if self._sent is not None:
            az, pitch = self._sent.az_servo, self._sent.pitch_servo
        else:
            az = self._boot_az if self._boot_az is not None else config.AZ_SERVO_AT_REF
            if tel.has(ST_IMU) and config.TILT_MIN_DEG >= 0.0:
                pitch = solar.tilt_to_servo(tel.imu_tilt)       # misura reale
            elif self._boot_pitch is not None:
                pitch = self._boot_pitch
            else:
                pitch = solar.tilt_to_servo(config.STOW_TILT_DEG)
        self.bridge.call("sync_position", _cdeg(az), _cdeg(pitch))
        log.info("posizione iniziale: servo azimut %.0f, servo pitch %.0f", az, pitch)

    def _check_pitch(self, decision: Decision, tel: Telemetry | None, ts: float) -> None:
        """Confronta l'inclinazione comandata con quella misurata dal Modulino."""
        if (tel is None or self._sent is None or not tel.has(ST_IMU) or tel.has(ST_MOVING)
                or not tel.has(ST_SERVO_PITCH) or ts - self._sent_at < 15.0):
            return
        error = tel.imu_tilt - abs(self._sent.tilt)
        if abs(error) > config.PITCH_MISMATCH_DEG:
            self._warn("pitch", ts, f"inclinazione misurata {tel.imu_tilt:.0f} contro {abs(self._sent.tilt):.0f} "
                                    "comandata: servo bloccato o taratura da rivedere")

    # ------------------------------------------------------------- modello AI

    def _check_plant(self, decision: Decision, ts: float) -> None:
        if ts < self._next_plant or decision.sun.elevation < 10.0:
            return
        self._next_plant = ts + config.PLANT_CHECK_S
        try:
            image = self.plant_camera.capture()
            if image is not None:
                self.controller.report_plant(self.model.classify_plant(image))
        except Exception as exc:
            self._warn("pianta", ts, f"riconoscimento pianta non riuscito: {exc}")

    # ------------------------------------------------------------- irrigazione

    def _check_irrigation(self, decision: Decision, tel: Telemetry | None, wx, now: datetime) -> None:
        ts = now.timestamp()
        try:
            request = self.irrigation.step(now, decision.sun.elevation, tel, wx, self.controller.plant)
            if request is None:
                if self.irrigation.blocked == "serbatoio vuoto":
                    self._warn("serbatoio", ts, "la pianta ha sete ma il serbatoio e' vuoto", every=6 * 3600)
                return
            lines = int(config.VALVE_LINES)
            each = max(1, -(-int(request.seconds) // lines))        # la dose divisa tra le linee, per eccesso
            granted = int(self.bridge.call("water", (1 << lines) - 1, each))
            if granted > 0:
                ml = self.irrigation.confirm(ts, granted * lines)
                self._plan_valves((1 << lines) - 1, granted, in_sequence=True)
                simulated = tel is not None and not tel.has(ST_VALVE)
                self._event(f"Innaffiatura: {ml:.0f} ml sulle {lines} linee, una dopo l'altra ({request.reason})")
                log.info("INNAFFIO %.0f ml, %d linee da %d s l'una (%s) | serbatoio %.1f l%s", ml, lines, granted,
                         request.reason, self.irrigation.tank_ml / 1000.0,
                         " | LED AL POSTO DELLE VALVOLE: nessuna acqua erogata" if simulated else "")
            else:
                self.irrigation.refused(ts, granted)
                self._warn("valvola", ts, f"il microcontrollore non ha aperto la valvola (codice {granted})")
        except Exception as exc:
            self._warn("irrigazione", ts, f"irrigazione non riuscita: {exc}")

    # ---------------------------------------------------- valvola a mano
    VALVE_REFUSALS = {-1: "serbatoio vuoto", -2: "pausa minima tra due aperture", -3: "irrigazione gia' in corso"}

    def water_now(self, seconds: float, lines: int | None = None) -> dict:
        """Dalla pagina web: apre insieme le linee della maschera (bit 0 = linea 1) per i secondi chiesti.

        Senza maschera vale per tutte le linee; secondi a zero = chiudi tutto.
        """
        everything = (1 << int(config.VALVE_LINES)) - 1
        mask = everything if lines is None else int(lines) & everything
        seconds = max(0, min(int(config.WATER_MAX_OPEN_S), int(round(float(seconds)))))
        self._valve_cmd = (mask, seconds)
        return {"queued": True, "seconds": seconds, "lines": mask}

    def _plan_valves(self, mask: int, seconds: int, in_sequence: bool) -> None:
        """Segna quando apre e chiude ogni linea, per la pagina e per i conti se si chiude prima."""
        start = self._clock()
        self._valve_plan = {}
        for line in range(int(config.VALVE_LINES)):
            if mask & (1 << line):
                self._valve_plan[line] = (start, start + seconds)
                if in_sequence:
                    start += seconds

    def _valve_lines(self, tel: Telemetry | None, now: float) -> list:
        """Per ogni linea, se e' aperta: dalla scheda se parla, altrimenti dal piano."""
        count = int(config.VALVE_LINES)
        if tel is not None:
            return [bool(tel.status & (ST_VALVE_LINE1 << line)) for line in range(count)]
        return [line in self._valve_plan and self._valve_plan[line][0] <= now < self._valve_plan[line][1]
                for line in range(count)]

    def _manual_valve(self, mask: int, seconds: int, ts: float) -> None:
        try:
            now = self._clock()
            if seconds <= 0 or mask == 0:
                self.bridge.call("water_manual", 0, 0)
                unused = sum(max(0.0, stop - max(now, start)) for start, stop in self._valve_plan.values())
                if unused > 0.5:
                    self.irrigation.refund(unused)
                self._valve_plan = {}
                self._event("Valvole chiuse a mano")
                log.info("valvole chiuse a mano")
                return
            names = ", ".join(str(line + 1) for line in range(int(config.VALVE_LINES)) if mask & (1 << line))
            granted = int(self.bridge.call("water_manual", mask, seconds))
            if granted > 0:
                count = bin(mask).count("1")
                unused = sum(max(0.0, stop - max(now, start)) for start, stop in self._valve_plan.values())
                if unused > 0.5:
                    self.irrigation.refund(unused)          # il comando a mano sostituisce quello in corso
                ml = self.irrigation.confirm(ts, granted * count)
                self._plan_valves(mask, granted, in_sequence=False)
                what = f"linea {names}" if count == 1 else f"linee {names}"
                self._event(f"Innaffiatura a mano: {what}, {granted} s, {ml:.0f} ml")
                log.info("INNAFFIO A MANO %s: %.0f ml in %d s | serbatoio %.1f l", what, ml, granted,
                         self.irrigation.tank_ml / 1000.0)
            else:
                why = self.VALVE_REFUSALS.get(granted, f"codice {granted}")
                self._event(f"Valvole non aperte: {why}")
                log.warning("comando a mano: valvole non aperte (%s)", why)
        except Exception as exc:
            self._event("Valvole: comando non riuscito")
            self._warn("valvola_mano", ts, f"comando a mano delle valvole non riuscito: {exc}", every=0.0)

    # ------------------------------------------------- prova dei componenti
    def debug_command(self, action: str, **args) -> dict:
        """Comandi della pagina "Prova componenti". Vengono eseguiti al prossimo giro del ciclo."""
        now = self._clock()
        if action == "stop":
            if self._debug is not None:
                self._debug["until"] = 0.0          # il ciclo chiude la prova e riparte in automatico
            return {"active": False}
        if action == "camera":
            return self._debug_camera()
        debug = self._debug
        if debug is None:
            debug = self._debug = {"until": 0.0, "awake": False, "az": None, "pitch": None, "dirty": False,
                                   "sent_at": 0.0, "leds": None, "ask_stage": False}
            self._manual = None
            self._event("Prova dei componenti avviata: automatismo fermo")
            log.info("prova dei componenti avviata dalla pagina web")
        debug["until"] = now + config.DEBUG_TIMEOUT_S
        if action == "servo":
            axis = "az" if args.get("axis") == "az" else "pitch"
            debug[axis] = max(0.0, min(180.0, float(args["deg"])))
            debug["dirty"] = True
        elif action == "leds":
            debug["leds"] = max(0, min(63, int(args.get("mask", 0))))
        elif action == "stage":
            debug["ask_stage"] = True
        elif action != "start":
            return {"error": f"Comando sconosciuto: {action}"}
        return {"active": True}

    def _debug_tick(self, debug: dict, wall: float, ts: float) -> None:
        try:
            if not debug["awake"]:
                debug["awake"] = True
                if self.low_power:
                    self.bridge.call("set_low_power", 0)        # sveglia la scheda: servono sensori e LED
            with self._lock:
                tel = self._telemetry
            if debug["dirty"] or wall - debug["sent_at"] >= 20.0:    # ripetuto ogni 20 s: e' anche il "sono vivo"
                az = debug["az"] if debug["az"] is not None else (tel.az_servo if tel else config.AZ_SERVO_AT_REF)
                pitch = debug["pitch"] if debug["pitch"] is not None else \
                    (tel.pitch_servo if tel else solar.tilt_to_servo(config.STOW_TILT_DEG))
                moved = debug["dirty"]
                debug["dirty"], debug["sent_at"] = False, wall
                self.bridge.call("set_target", _cdeg(az), _cdeg(pitch))
                if moved:
                    log.info("prova servo: azimut %.0f, pitch %.0f (gradi servo)", az, pitch)
            if debug["leds"] is not None:
                mask, debug["leds"] = debug["leds"], None
                self.bridge.call("led_test", int(mask))
            if debug["ask_stage"]:
                debug["ask_stage"] = False
                stage = int(self.bridge.call("boot_stage", timeout=3))
                self._debug_stage = f"{time.strftime('%H:%M:%S')}  {BOOT_STAGES.get(stage, f'passo {stage}')}"
        except Exception as exc:
            self._debug_stage = f"{time.strftime('%H:%M:%S')}  la scheda non risponde: {exc}"
            self._warn("prova", ts, f"prova dei componenti: comando non riuscito ({exc})", every=30.0)

    def _debug_end(self, text: str) -> None:
        self._debug = None
        try:
            self.bridge.call("led_test", 0)
        except Exception:
            pass
        self._sent = None                           # i servo sono stati mossi a mano: si ricomanda la posa
        self._event(text)
        log.info("prova dei componenti terminata")
        self._kick = True

    def _debug_camera(self) -> dict:
        camera = self.test_camera or self.plant_camera
        try:
            image = camera.capture()
        except Exception as exc:
            return {"ok": False, "text": f"La camera ha dato errore: {exc}"}
        if image is None:
            return {"ok": False, "text": "Nessuna immagine: camera non collegata o non riconosciuta."}
        shape = getattr(image, "shape", None)
        size = f"{shape[1]} x {shape[0]} punti" if shape is not None and len(shape) >= 2 else "immagine ricevuta"
        try:
            plant = self.model.classify_plant(image)
            health = self.model.assess_health(image)
            seen = f" I modelli vedono: {plant.label} ({plant.confidence * 100:.0f}%), {health.label}."
        except Exception as exc:
            seen = f" Modelli non eseguiti: {exc}"
        return {"ok": True, "text": f"Camera funzionante, {size}.{seen}"}

    def _debug_state(self, tel: Telemetry | None, now: float) -> dict:
        debug = self._debug
        state = {"active": debug is not None,
                 "remaining_s": max(0, round(debug["until"] - now)) if debug else 0,
                 "stage": self._debug_stage, "log": list(self.mcu_log),
                 "servo": {"az": debug["az"] if debug else None, "pitch": debug["pitch"] if debug else None},
                 "models": {name: bool(getattr(self.model, "is_real", lambda n: False)(name))
                            for name in ("classify_plant", "assess_health")},
                 "weather": None, "sensors": None}
        wx = self._last_wx
        if wx is not None:
            state["weather"] = {"simulated": self.demo,
                                "age_min": None if self.demo else max(0, round((now - wx.fetched_at) / 60.0)),
                                "rain_mm_h": round(wx.rain_rate_mm_h, 1), "gust_ms": round(wx.gust_ms, 1)}
        if tel is not None:
            has = tel.has
            state["sensors"] = {
                "age_s": round(now - tel.received_at),
                "ambient": {"ok": has(ST_AHT_AMBIENT), "t": round(tel.t_ambient, 1), "rh": round(tel.rh_ambient)},
                "under": {"ok": has(ST_AHT_PLANT), "t": round(tel.t_plant, 1), "rh": round(tel.rh_plant)},
                "light": {"ok": has(ST_LIGHT), "raw": None if self._light_raw is None else round(self._light_raw),
                          "pct": round(tel.light_pct)},
                "ina": {"ok": has(ST_INA), "v": round(tel.bus_v, 2), "ma": round(tel.current_ma)},
                "soil": {"ok": has(ST_SOIL), "pct": round(tel.soil_pct)},
                "tank": {"ok": has(ST_TANK_SENSOR), "water": has(ST_TANK_OK)},
                "valve": {"ok": has(ST_VALVE), "open": has(ST_VALVE_OPEN), "lines": self._valve_lines(tel, now)},
                "servo_az": {"ok": has(ST_SERVO_AZ), "deg": round(tel.az_servo, 1)},
                "servo_pitch": {"ok": has(ST_SERVO_PITCH), "deg": round(tel.pitch_servo, 1)},
                "moving": has(ST_MOVING), "failsafe": has(ST_FAILSAFE), "synced": has(ST_SYNCED),
            }
        return state

    # ------------------------------------------------------- foto e pagina web

    def _event(self, text: str) -> None:
        when = self._last_now or datetime.now(timezone.utc)
        stamp = f"{when + timedelta(hours=2):%H:%M}"
        for old in list(self.events)[:6]:           # lo stesso fatto ripetuto di fila si conta, non si riscrive
            if old["text"] == text:
                old["count"] = old.get("count", 1) + 1
                old["time"] = stamp
                return
        self.events.appendleft({"time": stamp, "text": text, "count": 1})

    def analyze_photo(self, image) -> dict:
        """Passa una foto ai due modelli (tipo di pianta e salute) e applica il risultato.

        Chiamata dall'interfaccia web quando si carica una foto a mano.
        """
        plant = self.model.classify_plant(image)
        health = self.model.assess_health(image)
        actions = []

        known = plant.best_label(config.PLANT_MIN_CONFIDENCE)
        accepted = known is not None
        if accepted:
            self.controller.report_plant(plant)
            profile = self.controller.plant
            if known != plant.label:
                actions.append(f"Specie incerta ({plant.confidence * 100:.0f}%), ma il genere {known} "
                               f"e' riconosciuto al {plant.group_confidence * 100:.0f}%")
            if profile.name == "sconosciuta":
                actions.append(f"\"{known}\" non ha un profilo in plants.py: uso i valori generici "
                               f"({profile.water_ml_day} ml di acqua al giorno, luce minima {profile.light_min_pct}%)")
            else:
                actions.append(f"Profilo impostato su {profile.name}: {profile.water_ml_day} ml di acqua al giorno, "
                               f"luce minima {profile.light_min_pct}%")
        else:
            actions.append(f"Riconoscimento troppo incerto ({plant.confidence * 100:.0f}%): profilo non cambiato")
        profile = self.controller.plant

        health_ok = health.confidence >= config.HEALTH_MIN_CONFIDENCE
        if not health_ok:
            actions.append(f"Valutazione della salute troppo incerta ({health.confidence * 100:.0f}%): nessuna azione")
        elif health.needs_water:
            self.irrigation.force_reason = f"controllo salute: {health.label}"
            actions.append("La pianta ha sete: innaffiatura immediata richiesta")
        elif not health.healthy:
            actions.append("Pianta da controllare: nessuna azione automatica, segnalata qui")
        else:
            actions.append("Pianta sana: irrigazione normale")

        self._event(f"Foto analizzata: {plant.label} ({plant.confidence * 100:.0f}%), "
                    f"salute: {health.label} ({health.confidence * 100:.0f}%)")
        log.info("foto: pianta %s %.0f%%%s | salute %s %.0f%%%s | %s", plant.label, plant.confidence * 100,
                 " (simulato)" if plant.simulated else "", health.label, health.confidence * 100,
                 " (simulato)" if health.simulated else "", "; ".join(actions))
        self.photo = {
            "plant": {"label": plant.label, "confidence": round(plant.confidence, 3), "accepted": accepted,
                      "simulated": plant.simulated, "group": plant.group,
                      "group_confidence": round(plant.group_confidence, 3),
                      "alternatives": [{"label": n, "confidence": round(c, 3)} for n, c in plant.alternatives]},
            "health": {"label": health.label, "confidence": round(health.confidence, 3),
                       "healthy": health.healthy, "needs_water": health.needs_water,
                       "accepted": health_ok, "simulated": health.simulated},
            "profile": {"name": profile.name, "light": profile.light, "water_ml_day": profile.water_ml_day,
                        "max_temp_c": profile.max_temp_c},
            "actions": actions,
        }
        self._kick = True           # reagisce subito, senza aspettare il prossimo giro
        return self.photo

    # --------------------------------------------------------- comando manuale

    def manual_limits(self) -> dict:
        """Corsa utile dei due assi, per i cursori della pagina."""
        a, b = solar.servo_to_az(config.AZ_SERVO_MIN), solar.servo_to_az(config.AZ_SERVO_MAX)
        return {"az_min": round(min(a, b)), "az_max": round(max(a, b)),
                "tilt_min": round(config.TILT_MIN_DEG), "tilt_max": round(config.TILT_MAX_DEG)}

    def set_manual(self, azimuth: float, tilt: float) -> dict:
        """Porta il pannello dove chiede la pagina web, fino a scadenza o a `set_auto`."""
        pose = solar.make_pose(float(azimuth), float(tilt))     # rispetta i limiti meccanici
        first = self._manual is None
        self._manual = {"azimuth": pose.azimuth, "tilt": pose.tilt,
                        "until": self._clock() + config.MANUAL_TIMEOUT_S}
        if first:
            self._event("Comando manuale dalla pagina web")
        log.info("comando manuale: azimut %.0f, tilt %.0f", pose.azimuth, pose.tilt)
        self._kick = True           # si muove subito (in basso consumo: sveglia la scheda)
        return self._manual_state()

    def set_auto(self) -> dict:
        if self._manual is not None:
            self._manual = None
            self._event("Ritorno in automatico")
            log.info("comando manuale terminato: ritorno in automatico")
            self._kick = True
        return self._manual_state()

    def _manual_state(self) -> dict:
        manual = self._manual
        state = {"active": manual is not None, **self.manual_limits()}
        if manual is not None:
            state.update(azimuth=round(manual["azimuth"]), tilt=round(manual["tilt"]),
                         remaining_s=max(0, round(manual["until"] - self._clock())))
        return state

    def snapshot(self) -> dict:
        """Stato corrente per la pagina web (aggiornato a ogni decisione)."""
        snap = dict(self._snapshot)
        snap["events"] = list(self.events)[:12]
        snap["photo"] = self.photo
        snap["manual"] = self._manual_state()
        tel = self.telemetry(self._clock())     # la luce si aggiorna a ogni telemetria, non a ogni decisione
        now = self._clock()
        with self._lock:
            last = self._telemetry              # anche vecchia: in basso consumo la scheda tace tra due risvegli
        snap["link"] = {"age_s": None if last is None else round(now - last.received_at),
                        "fresh": tel is not None, "asleep": bool(self.low_power and self._debug is None)}
        snap["debug"] = self._debug_state(last, now)
        left = max([0.0] + [stop - now for _, stop in self._valve_plan.values()])
        lines_open = self._valve_lines(tel, now)
        snap["valve"] = {"open": any(lines_open) or bool(tel is not None and tel.has(ST_VALVE_OPEN)),
                         "lines": lines_open, "remaining_s": round(left), "max_s": int(config.WATER_MAX_OPEN_S),
                         "flow_ml_s": config.VALVE_FLOW_ML_S,
                         "simulated": bool(last is not None and not last.has(ST_VALVE))}
        profile = self.controller.plant
        seen = tel if tel is not None else (last if self.low_power else None)
        snap["light"] = {"pct": round(seen.light_pct) if seen is not None and seen.has(ST_LIGHT) else None,
                         "stale": tel is None and seen is not None,
                         "digital": bool(last is not None and last.has(ST_LIGHT_DIGITAL)),
                         "min": profile.light_min_pct,
                         "max": profile.light_min_pct + round(config.LIGHT_HYSTERESIS_PCT),
                         "enabled": bool(config.PLANT_LIGHT_ENABLED), **self._light_state()}
        return snap

    def _build_snapshot(self, decision: Decision, tel: Telemetry | None, wx, now: datetime) -> dict:
        pose = self._sent or decision.pose
        power, measured = self.power_w(decision, tel)
        profile = self.controller.plant
        snap = {
            "ready": True,
            "demo": self.demo,
            "low_power": self.low_power,
            "wake_minutes": round(self.wake_period / 60.0),
            "time": f"{now + timedelta(hours=2):%H:%M}",
            "mode": decision.mode,
            "reason": decision.reason,
            "sun": {"elevation": round(decision.sun.elevation, 1), "azimuth": round(decision.sun.azimuth, 1)},
            "panel": {"azimuth": round(pose.azimuth, 1), "tilt": round(pose.tilt, 1),
                      "power_w": round(power, 2), "power_measured": measured,
                      "open_pct": round(decision.light_share * 100)},
            "raining": decision.raining,
            "weather": None if wx is None else {"rain_mm_h": round(wx.rain_rate_mm_h, 1),
                                                "gust_ms": round(wx.gust(now.timestamp()), 1),
                                                "wind_dir": round(wx.wind_dir)},
            "plant": {"name": profile.name, "label": self.controller.s["plant"], "light": profile.light,
                      "water_ml_day": profile.water_ml_day},
            "irrigation": {"deficit_ml": round(self.irrigation.deficit_ml),
                           "tank_ml": round(self.irrigation.tank_ml),
                           "tank_capacity_ml": round(config.TANK_CAPACITY_ML),
                           "today_ml": round(self.irrigation.s["water_today_ml"]),
                           "blocked": self.irrigation.blocked,
                           "pending": self.irrigation.force_reason},
            "mcu": None,
        }
        if tel is not None:
            snap["mcu"] = {
                "ambient": {"t": round(tel.t_ambient, 1), "rh": round(tel.rh_ambient)} if tel.has(ST_AHT_AMBIENT) else None,
                "under": {"t": round(tel.t_plant, 1), "rh": round(tel.rh_plant)} if tel.has(ST_AHT_PLANT) else None,
                "imu_tilt": round(tel.imu_tilt) if tel.has(ST_IMU) else None,
                "valve_open": tel.has(ST_VALVE_OPEN),
                "moving": tel.has(ST_MOVING),
                "failsafe": tel.has(ST_FAILSAFE),
                "stubs": [n for bit, n in ((ST_INA, "INA219"), (ST_SERVO_AZ, "servo azimut"),
                                           (ST_SERVO_PITCH, "servo pitch"), (ST_VALVE, "elettrovalvola"),
                                           (ST_SOIL, "sensore terreno")) if not tel.has(bit)],
            }
        return snap

    # ---------------------------------------------------------------------- log

    def power_w(self, decision: Decision, tel: Telemetry | None) -> tuple[float, bool]:
        """(potenza del pannello in W, True se misurata dall'INA219)."""
        if tel and tel.has(ST_INA):
            return tel.bus_v * tel.current_ma / 1000.0, True
        pose = self._sent or decision.pose
        poa = solar.poa_irradiance(decision.sun, decision.dni, decision.dhi, pose.azimuth, pose.tilt)
        return config.PANEL_PEAK_W * poa / 1000.0, False

    def _log_status(self, decision: Decision, tel: Telemetry | None, ts: float) -> None:
        if self._wall - self._last_status < self.status_period:
            return
        self._last_status = self._wall
        power, measured = self.power_w(decision, tel)
        parts = [f"{self.time_label(decision.when)}{decision.mode}",
                 f"sole el {decision.sun.elevation:.0f} az {decision.sun.azimuth:.0f}",
                 f"potenza {power:.2f} W ({'misurata' if measured else 'stimata'})",
                 f"pianta {self.controller.plant.name}"]
        if tel:
            if tel.has(ST_AHT_AMBIENT):
                parts.append(f"aria {tel.t_ambient:.1f} C {tel.rh_ambient:.0f}%")
            if tel.has(ST_AHT_PLANT):
                parts.append(f"sotto {tel.t_plant:.1f} C {tel.rh_plant:.0f}%")
            if tel.has(ST_IMU):
                parts.append(f"tilt IMU {tel.imu_tilt:.0f}")
            if tel.has(ST_LIGHT):
                parts.append(f"luce {tel.light_pct:.0f}%")
            stub = [n for bit, n in ((ST_INA, "INA"), (ST_SERVO_AZ, "servo azimut"),
                                     (ST_SERVO_PITCH, "servo pitch"), (ST_VALVE, "valvola"),
                                     (ST_SOIL, "sensore terreno")) if not tel.has(bit)]
            if stub:
                parts.append("stub: " + ", ".join(stub))
            parts.append(f"sete {self.irrigation.deficit_ml:.0f} ml"
                         + (f", terreno {tel.soil_pct:.0f}%" if tel.has(ST_SOIL) else "")
                         + (f" ({self.irrigation.blocked})" if self.irrigation.blocked else ""))
            if tel.has(ST_FAILSAFE):
                parts.append("MCU in sicurezza")
        log.info(" | ".join(parts))

    def _warn(self, key: str, ts: float, message: str, every: float = 600.0) -> None:
        # la frequenza degli avvisi segue l'orologio reale, anche in dimostrazione
        if self._wall - self._warned.get(key, -1e12) >= every:
            self._warned[key] = self._wall
            log.warning(message)
