# Inseguitore solare intelligente (Arduino UNO Q)

App per Arduino App Lab: pannello a 2 assi (azimut + pitch) che insegue il sole,
si apre quando alla pianta che ha sotto arriva poca luce (fotoresistore),
riconosce la pianta e il suo stato di salute da una foto e la innaffia dal
serbatoio di raccolta quando ne ha bisogno. Una pagina web mostra tutto e
permette di muovere il pannello a mano.

```
app.yaml               nome dell'app e brick WebUI
sketch/sketch.ino      microcontrollore: sensori, servo, LED, valvola, sicurezza
sketch/sketch.yaml     librerie
assets/index.html      pagina web (porta 7000)
python/main.py         avvio dell'app sul lato Linux
python/config.py       TUTTA la taratura (coordinate, meccanica, soglie)
python/controller.py   logica di decisione
python/tracker.py      collega telemetria, meteo, modelli, servo e pagina web
python/webui.py        indirizzi della pagina web (/state, /photo, /manual, /auto)
python/solar.py        posizione del sole e ricerca della posa migliore
python/weather.py      previsioni Open-Meteo con cache
python/ai_model.py     interfaccia verso i due modelli AI + sostituti simulati
python/colleague_model.py  carica i modelli ONNX veri da python/models/
python/requirements.txt    librerie Python in più (onnxruntime)
python/irrigation.py   quando e quanto innaffiare
python/demo.py         giornata simulata per le dimostrazioni
python/devices.py      camera della pianta
python/plants.py       profili delle piante (acqua, luce minima, temperatura)
tests/test_logic.py    prove della logica su PC, senza scheda
```

## Pinout

**La UNO Q lavora a 3,3 V.** Molti pin digitali sopportano 5 V in ingresso, ma non D3, A0 e A1
(massimo 3,6 V) né gli altri pin analogici quando leggono.

### Con il Grove Base Shield (Seeed), interruttore su 5V

Lo sketch è impostato così (`SHIELD_5V 1`). A 5 V vale una regola: **niente che mandi un
segnale sul pin D3 e niente segnali analogici**. Per questo l'AHT20 della pianta non sta sulla
porta D2 e il fotoresistore si legge dalla sua uscita digitale.

| Porta dello shield | Filo giallo | Filo bianco | Note |
|---|---|---|---|
| **I2C** (una qualsiasi) | AHT20 n.1 "aria esterna", cavo Grove intero | | SDA/SCL (D20/D21), I2C via software |
| **D2** | fotoresistore, uscita **DO** (D2) | **non collegare** (è D3) | rosso → VCC, nero → GND del modulino. AO resta scollegato |
| **D4** | LED stato (D4) | LED azimut (D5) | |
| **D6** | valvole, linea 1 (D6) | linea 2 (D7) | verso l'integrato pilota |
| **D8** | AHT20 n.2 "sotto il pannello", cavo Grove intero | | D8 = SCL, D9 = SDA, I2C via software |
| **A0** | LED luce pianta (A0) | LED meteo (A1) | gli stessi due LED escono in copia su D11 e D12 |
| **A2** | valvole, linea 3 (A2) | linea 4 (A3) | verso l'integrato pilota |
| pin della scheda | servo pitch: **D10** segnale, **5V**, **GND** | | l'unico che resta sui pin |
| **USB-C** | alimentazione, camera USB | | lato Linux |

Previsti nel firmware ma non collegati: servo azimut (D9), galleggiante (D13), umidità del
terreno (A4, solo a 3,3 V), INA219 (porta I2C).

- I due AHT20 hanno lo stesso indirizzo fisso (0x38): uno sulle porte I2C, l'altro sulla porta D8.
- D9 è anche il pin del secondo servo: quando verrà montato, l'AHT20 della pianta andrà
  spostato (per esempio sul Qwiic, che resta a 3,3 V: `PLANT_AHT_ON_SOFT_I2C 0`).
- Il fotoresistore a 5 V dà solo luce o buio; la soglia è la vite blu del modulino. Per avere
  la percentuale serve l'uscita AO su A1, quindi il modulino a 3,3 V oppure un partitore
  (AO → 10 kΩ → A1 → 20 kΩ → GND) e `LIGHT_DIGITAL 0` nello sketch.
- Sui fili rossi dei cavi Grove ci sono 5 V: ai LED non vanno collegati.
- Con l'interruttore su 3.3V: `SHIELD_5V 0`, AHT20 della pianta sulla porta D2, fotoresistore
  con AO su A1.

### LED

Accesi a livello alto. LED nudi: resistenza da 330 Ω – 1 kΩ in serie, catodo a GND.
Chi non serve si disattiva mettendo `-1` al posto del pin nello sketch.

| LED | Pin | Significato |
|---|---|---|
| Stato | D4 | 1 lampo ogni 2 s = tutto ok · 2 lampi = manca un AHT20 · lampeggio veloce = il lato Linux non sta comandando |
| Azimut | D5 | acceso mentre l'asse di azimut si muove: fa le veci del secondo servo finché non viene montato |
| Linee 1–4 | D6, D7, A2, A3 | nella scatola delle valvole: acceso = linea aperta. Sono le uscite vere delle elettrovalvole |
| Luce pianta | A0 (e D11) | fisso = pannello aperto per dare luce alla pianta · lampeggiante = comando manuale dalla pagina web |
| Meteo | A1 (e D12) | fisso = piove · lampeggio veloce = sicurezza vento |

C'è anche un LED per il pitch (`PIN_LED_PITCH`), spento di default.

### Servo ed elettrovalvole

Il servo è pilotato dal timer hardware della scheda (PWM su D9/D10), senza la
libreria Servo, che su questa scheda blocca lo sketch.

Servo SG90: arancione al pin di segnale, marrone a GND, rosso al pin **5V**
dell'header (va bene per il prototipo con un solo servo; mai dal 3V3 né dalle
porte Grove). Un elettrolitico da 470–1000 µF tra 5V e GND vicino al servo evita
i reset quando parte. Con due servo serve un'alimentazione a 5 V separata, con
GND in comune.

Elettrovalvole: quattro linee, su D6, D7, A2 e A3. I pin danno 3,3 V e pochi mA,
quindi comandano solo l'integrato pilota sulla millefori (oggi accende 4 LED,
domani le valvole vere con lo stesso circuito); le valvole hanno la loro
alimentazione. Livello alto = linea aperta (`VALVE_ACTIVE_HIGH`). Con le valvole
vere servono un diodo di ricircolo su ogni bobina, se l'integrato non li ha già,
e valvole che aprano **a pressione zero** (ad azione diretta): con un serbatoio
a caduta molte valvole da irrigazione a 12 V non aprono.

## Alimentazione

```
pannello → step-down → HW-736 → batteria LiPo 3,7 V → convertitore boost 5 V → pin 5V della UNO Q + servo
```

- La UNO Q vuole **5 V stabili, almeno 3 A** (pin 5V o USB-C; in alternativa 7–24 V su VIN).
- L'HW-736 risulta essere un caricatore CN3065: in uscita dà la tensione della
  batteria (3,7–4,2 V), non 5 V, quindi serve un boost a 5 V tra batteria e scheda.
- Lo stesso modulo carica al massimo a 500 mA (circa 2 W): da verificare contro il
  consumo della scheda, che è un computer Linux.
- Alimentando dal pin 5V, la porta USB-C resta libera per la camera.

## Componenti simulati

| Componente | Dove | Come si passa a quello vero |
|---|---|---|
| Servo mancante | `sketch.ino` | `#define HAS_SERVO_AZ 1` (o `HAS_SERVO_PITCH`) |
| Elettrovalvole | `sketch.ino` | le uscite funzionano già (LED). Con le valvole vere `#define HAS_VALVE 1`: cambia solo la scritta sulla pagina |
| Sensore di terreno | `sketch.ino` | `#define HAS_SOIL_SENSOR 1` e taratura `SOIL_RAW_DRY` / `SOIL_RAW_WET`. Senza, il fabbisogno è stimato |
| Galleggiante | `sketch.ino` | `#define HAS_TANK_SENSOR 1`. Senza, il livello del serbatoio è stimato dalla pioggia |
| INA219 | `sketch.ino` | niente: viene rilevato da solo all'indirizzo 0x40. Finché manca, la potenza è *stimata* dal modello solare |
| Fotoresistore | `sketch.ino` | è attivo; se non è montato `#define HAS_LIGHT_SENSOR 0` |

## Pagina web

Con l'app in esecuzione: `http://<nome-della-scheda>.local:7000`. Ha due pagine.

**Impianto**
- **Pannello** (riquadro scuro): modalità, motivo, disegno di sole e pannello, copia dei LED.
- **La pianta**: si carica una foto, i due modelli dicono che pianta è e come sta.
  Il tipo imposta il profilo (acqua, luce minima); se ha sete parte subito un'innaffiatura.
- **Luce alla pianta**: lettura del fotoresistore rispetto al minimo della pianta.
- **Irrigazione**: sete, serbatoio, acqua erogata, stato delle quattro linee e comando
  a mano (si scelgono le linee e i secondi, al massimo 60; si richiudono da sole o
  con "Chiudi tutto"; non vale la pausa di 5 minuti dell'automatico, resta il
  blocco a serbatoio vuoto).
- **Muovi il pannello a mano**: due cursori e pose rapide. Torna in automatico con
  il pulsante o dopo 10 minuti (`MANUAL_TIMEOUT_S`). Il vento forte vince comunque.

**Prova componenti** (per il banco, non per la demo)
- "Avvia la prova" ferma l'automatismo (anche la sicurezza vento) e tiene sveglia
  la scheda; si chiude con il pulsante o da sola dopo 10 minuti (`DEBUG_TIMEOUT_S`).
- **Servo**: angolo diretto del servo da 0 a 180°, senza passare dai calcoli del sole.
- **Elettrovalvole**: una linea alla volta, tutte insieme o una dopo l'altra, 3 secondi l'una.
- **LED**: accende un LED alla volta, tutti, o in sequenza (richiede lo sketch aggiornato).
- **Sensori**: valori in diretta dei due AHT20, del fotoresistore e
  dei componenti facoltativi, ognuno con "risponde", "non risponde" o "non montato".
- **Fotoresistore**: lettura grezza e taratura (buio, piena luce).
- **Camera e modelli**: scatto di prova e stato dei due modelli (vero o simulato).
- **Scheda**: età degli ultimi dati, a che punto è lo sketch, ultimi messaggi `[MCU]`.

## Modelli AI

Due modelli ONNX in `python/models/`, dichiarati in `config.py` (`HEALTH_MODEL_*`,
`PLANT_MODEL_*`). Entrambi sono MobileNetV3-Small con ingresso 224×224; le foto
vengono preparate nel modo standard di torchvision (`MODEL_RESIZE`, `MODEL_MEAN`,
`MODEL_STD`).

- **Tipo di pianta** (`plantnet_mobilenetv3.onnx`): 1081 specie del dataset
  Pl@ntNet-300K. I nomi scientifici sono in `plantnet_labels.txt`, uno per riga
  nell'ordine delle uscite (id di specie in ordine crescente, come nel codice
  ufficiale del dataset). **Non conosce basilico, pomodoro, peperone, spinacio**;
  conosce per esempio fragola, lattuga, zucca e zucchina, lavanda, geranio,
  calendula, tagete, piante grasse (Sedum, Lithops, cactus), felci e molte piante
  da interno (Anthurium, Alocasia, Peperomia, Fittonia, Zamioculcas, Schefflera,
  Tradescantia). Se la specie è incerta ma il genere è sicuro, vale il genere.
  Il profilo (acqua, luce, temperatura) si sceglie dal genere: tabella `GENUS`
  in `plants.py`; un genere senza profilo usa i valori generici.
- **Salute** (`plant_wilting.onnx`): due uscite. L'ordine delle etichette
  (`HEALTH_MODEL_LABELS = ("sana", "appassita")`) non è scritto nel file: **va
  confermato con chi l'ha addestrato**. Se una pianta sana risulta "appassita",
  scambiare le due etichette.
- `HEALTH_OK_KEYWORDS` e `HEALTH_THIRSTY_KEYWORDS` dicono quali etichette
  significano "sana" e "ha sete" (quest'ultima fa partire un'innaffiatura).
- Per eseguire i modelli serve `onnxruntime`, elencato in
  `python/requirements.txt` (App Lab lo installa al primo avvio, serve la rete).
  Se manca si prova con OpenCV; se non c'è nemmeno quello i modelli restano
  simulati, e la pagina lo scrive.

## Logica

A ogni decisione, in ordine di priorità:

1. **SICUREZZA** – raffica prevista ≥ 10 m/s: pannello piatto per almeno 10 minuti.
2. **MANUALE** – posa scelta dalla pagina web.
3. **NOTTE** – piatto.
4. **LUCE_PIANTA** – il fotoresistore legge meno del minimo della pianta
   (`light_min_pct` in `plants.py`): il pannello si apre un passo alla volta, da
   puntato al sole a messo di taglio, così la luce passa. Quando la luce supera
   il minimo + `LIGHT_HYSTERESIS_PCT`, o sotto il pannello fa più caldo del
   massimo della pianta, si richiude un passo alla volta.
5. **PRODUZIONE** – posa che massimizza l'irraggiamento previsto sul pannello:
   col sereno insegue il sole, col coperto si mette quasi piatto, con la pioggia
   resta inclinato almeno 10° per far scolare l'acqua.

La pioggia prevista viene confermata dall'umidità dell'AHT20 esterno. Se il lato
Linux smette di rispondere per 5 minuti il microcontrollore porta il pannello
in piano da solo.

Taratura della luce, dalla pagina web (riquadro "Luce alla pianta"): coprire il
sensore e premere "adesso è buio", scoprirlo e premere "adesso è piena luce". Da
lì la scala va da 0 (buio) a 100 (piena luce) qualunque sia il modulino, anche
se legge al contrario; resta salvata tra un avvio e l'altro. Le soglie delle
piante (`light_min_pct` in `plants.py`) sono percentuali di questa scala.

## Dimostrazione e basso consumo (`config.py`)

- `DEMO_MODE = True`: sole e meteo simulati, una giornata in circa 4 minuti
  (sereno, coperto, pioggia, raffiche). Sensori, fotoresistore, servo e LED sono
  quelli veri. **Rimettere `False` per l'uso reale.**
- `LOW_POWER_MODE = True` (vale solo con `DEMO_MODE = False`): l'impianto si
  sveglia ogni `WAKE_PERIOD_S` (30 minuti), legge i sensori, guarda le previsioni,
  decide, muove il pannello puntando dove sarà il sole a metà intervallo, poi
  toglie il segnale ai servo, spegne i LED e ferma le letture. Una foto o un
  comando manuale dalla pagina lo svegliano subito. Il processore Linux della
  scheda resta acceso: si risparmia su servo, LED e microcontrollore.

## Irrigazione

Il fabbisogno dipende dalla pianta riconosciuta (`water_ml_day` in `plants.py`).

- **Senza sensore di terreno**: bilancio idrico. Il fabbisogno giornaliero viene
  scalato con la secchezza dell'aria misurata dall'AHT20 sotto il pannello e si
  eroga una dose (150 ml) ogni volta che il debito accumulato la raggiunge.
- **Con il sensore di terreno**: si innaffia sotto la soglia `soil_min_pct` della pianta.
- Si evita il pieno sole (si rimanda alla sera, salvo urgenza), si aspetta
  30 minuti tra due dosi e non si supera il doppio del fabbisogno giornaliero.
- Il serbatoio si riempie con la pioggia (`COLLECTOR_AREA_M2`); a serbatoio vuoto
  non si apre e il log avvisa.
- Le linee sono quattro (`VALVE_LINES`): a ogni innaffiatura la dose viene divisa
  tra le linee, aperte una dopo l'altra.
- Sul microcontrollore ogni linea si richiude comunque da sola: al massimo 60 s
  per apertura e 5 minuti di pausa tra due aperture, anche se Linux si blocca.

Da tarare: `VALVE_FLOW_ML_S` (portata reale di una linea aperta), capienza del
serbatoio e superficie di raccolta. `RAIN_ON_PLANT_FRACTION` dice quanta pioggia
arriva alla pianta: è 0, cioè il pannello la copre.

## Diagnostica

Lo sketch non usa il monitor seriale: manda i suoi messaggi al lato Linux, che li
scrive nella console Python con il prefisso `[MCU]` (stato dei componenti ogni 30 s).
Se la telemetria non arriva, la console Python dice a che punto dell'avvio si è
fermato lo sketch.

## Taratura (`python/config.py`)

- `LATITUDE`, `LONGITUDE`: ora ci sono quelle di Roma.
- `AZ_SERVO_AT_REF`, `AZ_SERVO_DIR`: con il servo di base a 90° il pannello deve guardare a sud.
- `PITCH_SERVO_AT_FLAT`, `PITCH_SERVO_DIR`, `TILT_MAX_DEG`: angolo servo a pannello orizzontale e corsa.

## Prove

```
python tests/test_logic.py
```

Girano su PC con microcontrollore e meteo simulati.
