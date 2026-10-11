"""Parametri dell'impianto. Tutto quello che va tarato sta qui."""

import os

# --------------------------------------------------------------------------
# Sito  (DA CAMBIARE: coordinate di Roma come segnaposto)
# --------------------------------------------------------------------------
LATITUDE = 41.9028
LONGITUDE = 12.4964

# --------------------------------------------------------------------------
# Meccanica: da angoli "reali" ad angoli servo
#   azimut  = bussola, 0 = nord, 90 = est, 180 = sud, 270 = ovest
#   tilt    = inclinazione del pannello, 0 = piatto, 90 = verticale
# Un SG90 copre 180 gradi: con il servo a 90 il pannello guarda a sud e
# la base copre da est (servo 0) a ovest (servo 180).
# --------------------------------------------------------------------------
AZ_REF_DEG = 180.0          # azimut bussola quando il servo e' a AZ_SERVO_AT_REF
AZ_SERVO_AT_REF = 90.0
AZ_SERVO_DIR = 1.0          # -1 se il servo gira al contrario
AZ_SERVO_MIN = 0.0          # corsa utile del servo di base
AZ_SERVO_MAX = 180.0

PITCH_SERVO_AT_FLAT = 0.0   # angolo servo con il pannello parallelo alla base (tarato)
PITCH_SERVO_DIR = 1.0       # -1 se il servo gira al contrario
TILT_MIN_DEG = 0.0          # negativo solo se il pannello puo' ribaltarsi all'indietro
TILT_MAX_DEG = 135.0        # corsa tarata: oltre tocca (come PITCH_MAX_DEG nello sketch)

MOVE_DEADBAND_DEG = 1.5     # non muovere per correzioni piu' piccole di cosi'

# --------------------------------------------------------------------------
# Pannello (serve solo per stimare la potenza finche' l'INA219 e' uno stub)
# --------------------------------------------------------------------------
PANEL_PEAK_W = 5.0
ALBEDO = 0.2

# --------------------------------------------------------------------------
# Meteo (Open-Meteo, nessuna chiave)
# --------------------------------------------------------------------------
WEATHER_REFRESH_S = 30 * 60
WEATHER_MAX_AGE_S = 6 * 3600        # oltre questa eta' la previsione non si usa

RAIN_NOW_MM_H = 0.3                 # sopra questa intensita' "sta piovendo"
RAIN_CONFIRM_RH = 75.0              # e l'AHT20 esterno deve confermare (% UR)
RAIN_SOON_PROB = 70.0               # % di probabilita' nell'ora successiva
RAIN_SOON_MM = 0.5
RUNOFF_TILT_DEG = 10.0              # inclinazione minima quando piove

# --------------------------------------------------------------------------
# Sicurezza vento
# --------------------------------------------------------------------------
WIND_STOW_MS = 10.0                 # raffica oltre cui il pannello va piatto
WIND_RELEASE_MS = 7.0
SHAKE_STOW_DPS = 25.0               # vibrazione misurata dal Modulino
STOW_HOLD_S = 10 * 60               # tempo minimo in posizione di sicurezza
STOW_TILT_DEG = 0.0

# --------------------------------------------------------------------------
# Notte
# --------------------------------------------------------------------------
NIGHT_ELEVATION_DEG = -1.0
NIGHT_TILT_DEG = 0.0

# --------------------------------------------------------------------------
# Pianta (camera sotto il pannello + modello AI)
# --------------------------------------------------------------------------
PLANT_CHECK_S = 6 * 3600
PLANT_MIN_CONFIDENCE = 0.5          # sulla specie oppure, se la specie e' incerta, sul genere
PLANT_CAMERA_SOURCE = None          # None = prima camera trovata, oppure "usb:0"

# Modelli AI (file ONNX in python/models/). Con None il modello e' simulato.
# Le etichette vanno NELL'ORDINE DELLE USCITE del modello.
HEALTH_MODEL_FILE = "plant_wilting.onnx"
HEALTH_MODEL_LABELS = ("sana", "appassita")     # uscita 0, uscita 1: DA CONFERMARE con chi l'ha addestrato
# Tipo di pianta: MobileNetV3 addestrato su Pl@ntNet-300K, 1081 specie. Le etichette
# (nomi scientifici, una per riga nell'ordine delle uscite) stanno in un file.
PLANT_MODEL_FILE = "plantnet_mobilenetv3.onnx"
PLANT_MODEL_LABELS = "plantnet_labels.txt"
# Preparazione delle foto (quella standard di torchvision: lato corto a 256,
# ritaglio centrale 224, normalizzazione ImageNet)
MODEL_RESIZE = 256
MODEL_INPUT_SIZE = 224
MODEL_MEAN = (0.485, 0.456, 0.406)
MODEL_STD = (0.229, 0.224, 0.225)

# Salute della pianta: parole che, trovate nell'etichetta del modello, dicono
# "pianta sana" oppure "ha sete" (in questo caso parte subito un'innaffiatura).
# Vanno adattate alle etichette vere del modello.
HEALTH_OK_KEYWORDS = ("san", "healthy", "ok", "buon", "good")
HEALTH_THIRSTY_KEYWORDS = ("secc", "sete", "appass", "disidrat", "idric", "dry", "wilt", "thirst", "water")
HEALTH_MIN_CONFIDENCE = 0.5

# Luce alla pianta (fotoresistore accanto alla pianta, sotto il pannello).
# Se la luce letta scende sotto il minimo del profilo della pianta (plants.py)
# il pannello si "apre" un passo alla volta: da puntato al sole (massima
# produzione, massima ombra) a messo di taglio (la luce passa). Quando la luce
# supera il minimo + LIGHT_HYSTERESIS_PCT, o sotto il pannello fa troppo caldo,
# si richiude un passo alla volta per tornare a produrre.
PLANT_LIGHT_ENABLED = True
LIGHT_STEP = 0.25                   # quanto apre o chiude a ogni decisione (1 = tutta la corsa)
LIGHT_HYSTERESIS_PCT = 15.0
# La scala 0-100 si tara dalla pagina web (pulsanti "E' buio" / "E' piena luce"):
# tra le due letture grezze devono esserci almeno questi punti, altrimenti la
# taratura viene ignorata.
LIGHT_CAL_MIN_SPAN = 5.0

# --------------------------------------------------------------------------
# Irrigazione (serbatoio a caduta + elettrovalvola)
# --------------------------------------------------------------------------
IRRIGATION_ENABLED = True
VALVE_LINES = 4                     # linee di irrigazione, una valvola ciascuna (come VALVE_COUNT nello sketch)
VALVE_FLOW_ML_S = 10.0              # portata di UNA linea aperta: DA MISURARE con un bicchiere graduato
WATER_DOSE_ML = 150.0               # acqua per singola innaffiatura, divisa tra le linee (aperte una dopo l'altra)
WATER_MAX_OPEN_S = 60               # apertura massima di una linea, come VALVE_MAX_OPEN_MS nello sketch
WATER_MIN_INTERVAL_S = 30 * 60      # pausa tra due innaffiature (l'acqua deve assorbirsi)
WATER_DAILY_CAP_FACTOR = 2.0        # mai piu' di N volte il fabbisogno giornaliero della pianta
WATER_HOT_ELEVATION_DEG = 35.0      # col sole piu' alto di cosi' si rimanda alla sera...
WATER_URGENT_DOSES = 3.0            # ...a meno che manchino gia' N dosi
VPD_REFERENCE_KPA = 1.0             # secchezza dell'aria a cui vale water_ml_day del profilo

TANK_CAPACITY_ML = 5000.0
TANK_INITIAL_ML = 5000.0            # livello assunto al primo avvio
COLLECTOR_AREA_M2 = 0.10            # superficie che convoglia la pioggia nel serbatoio
COLLECTOR_EFFICIENCY = 0.8
PLANT_AREA_M2 = 0.05                # superficie del vaso
RAIN_ON_PLANT_FRACTION = 0.0        # quota di pioggia che arriva alla pianta (0 = il pannello la copre)

# --------------------------------------------------------------------------
# Controllo
# --------------------------------------------------------------------------
DECISION_PERIOD_S = 60
TELEMETRY_STALE_S = 15
PITCH_MISMATCH_DEG = 8.0            # scarto tollerato tra comando e IMU

# --------------------------------------------------------------------------
# Comando manuale dalla pagina web: dopo questo tempo senza nuovi comandi
# l'impianto torna da solo in automatico. Vento forte e scosse vincono sempre.
# --------------------------------------------------------------------------
MANUAL_TIMEOUT_S = 10 * 60
# Prova dei componenti dalla pagina web: finche' e' attiva l'automatismo e' fermo;
# si spegne da sola dopo questo tempo senza comandi.
DEBUG_TIMEOUT_S = 10 * 60

# --------------------------------------------------------------------------
# Basso consumo: l'impianto "dorme" e si sveglia ogni WAKE_PERIOD_S. A ogni
# risveglio legge i sensori, guarda le previsioni, decide e muove il pannello;
# poi toglie il segnale ai servo, spegne i LED e ferma le letture.
# Con False decide una volta al minuto e tiene tutto acceso.
# (Non vale in dimostrazione.)
# --------------------------------------------------------------------------
LOW_POWER_MODE = True
WAKE_PERIOD_S = 30 * 60
SERVO_SPEED_DEG_S = 30.0            # come nello sketch: serve a sapere quanto aspettare il movimento

# --------------------------------------------------------------------------
# Dimostrazione: una giornata finta in pochi minuti (vedi demo.py)
# Con True il sole e il meteo sono SIMULATI: rimettere False per l'uso reale.
# --------------------------------------------------------------------------
DEMO_MODE = True
DEMO_SPEED = 240.0                  # 1 secondo reale = 4 minuti simulati (una giornata in ~4 minuti)
DEMO_STEP_S = 2.0                   # secondi reali tra una decisione e l'altra

# --------------------------------------------------------------------------
# Simulazione dei componenti mancanti
# --------------------------------------------------------------------------
SIM_PLANT_LABEL = "basilico"

DATA_DIR = os.environ.get("TRACKER_DATA_DIR", "/app/data")
