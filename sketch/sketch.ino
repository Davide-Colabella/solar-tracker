/*
 * Inseguitore solare a 2 assi - lato MCU (STM32U585 della Arduino UNO Q)
 *
 * Compiti del microcontrollore:
 *   - leggere i due Grove AHT20 (stesso indirizzo 0x38 -> uno per bus I2C)
 *   - accendere i LED di stato
 *   - leggere l'INA219 (tensione/corrente pannello) se presente
 *   - leggere il fotoresistore (luce che arriva alla pianta)
 *   - muovere i due servo SG90 con rampa, limiti e posizione di sicurezza
 *   - aprire le 4 elettrovalvole dell'irrigazione (oggi 4 LED) per un tempo limitato
 *   - parlare con il lato Linux tramite Bridge (RPC); anche i messaggi di
 *     stato vanno li' e compaiono nella console Python con il prefisso [MCU]
 *
 * Tutta la "intelligenza" (sole, meteo, modello AI) sta in python/main.py.
 *
 * ---------------------------------------------------------------------------
 * PINOUT, con il Grove Base Shield e il suo INTERRUTTORE SU 5V (SHIELD_5V 1)
 * ---------------------------------------------------------------------------
 *  La UNO Q lavora a 3,3 V. Molti pin digitali sopportano 5 V in ingresso, ma
 *  NON D3, A0, A1 (massimo 3,6 V) ne' gli altri pin analogici quando leggono.
 *  Con la shield a 5 V quindi: niente su D3 e niente segnali analogici.
 *
 *  Bus I2C su SDA (D20) / SCL (D21) = porte "I2C" del Base Shield
 *  (gestito via software, non puo' bloccare lo sketch)
 *      0x38  Grove AHT20 n.1 "AMBIENTE" (aria esterna)
 *      0x40  INA219 (monitor I/V pannello)        [STUB]
 *
 *  Bus I2C software su D4 (SCL) e D5 (SDA) = porta "D4" del Base Shield
 *      0x38  Grove AHT20 n.2 "PIANTA" (sotto il pannello)
 *      I due AHT20 hanno lo stesso indirizzo: devono stare su due bus diversi.
 *      A 5 V NON va sulla porta D2: porterebbe 5 V sul pin D3.
 *
 *  D2   FOTORESISTORE, uscita digitale DO del modulino (alta = buio).
 *       Porta "D2", solo filo giallo; il filo bianco (D3) resta scollegato.
 *       L'uscita analogica AO NON va collegata: a 5 V brucerebbe A1.
 *
 *  D9   segnale servo AZIMUT (rotazione della base attorno a Z)
 *  D10  segnale servo PITCH  (inclinazione del pannello)
 *       Rosso di entrambi a un'alimentazione a 5 V separata (almeno 2 A),
 *       marrone a GND, con GND in comune con la scheda. Mai dal 3V3 ne' dalle
 *       porte Grove. Condensatore elettrolitico 470-1000 uF tra 5V e GND
 *       vicino ai servo.
 *
 *  ELETTROVALVOLE: 4 linee di irrigazione, comandate da un integrato pilota
 *  (oggi accende 4 LED, domani le valvole vere con lo stesso circuito).
 *  Due cavi Grove verso la scatola delle valvole:
 *      porta A0  ->  A0 (giallo) = linea 1,  A1 (bianco) = linea 2
 *      porta A2  ->  A2 (giallo) = linea 3,  A3 (bianco) = linea 4
 *  Livello alto = valvola aperta (VALVE_ACTIVE_HIGH). I pin danno 3,3 V e pochi
 *  mA: sempre attraverso l'integrato pilota, mai una valvola direttamente.
 *  D13  galleggiante del serbatoio verso GND (chiuso = c'e' acqua)   [STUB]
 *  A4   sensore capacitivo di umidita' del terreno, solo a 3,3 V    [STUB]
 *
 *  LED (accesi a livello alto; LED nudi con resistenza 330 ohm - 1 k in serie)
 *  D8   STATO     1 lampo ogni 2 s = tutto ok; 2 lampi = manca un sensore;
 *                 lampeggio veloce = il lato Linux non sta comandando
 *  D6   PIANTA    fisso = pannello aperto per dare luce alla pianta;
 *                 lampeggiante = comando manuale dalla pagina web
 *  D7   METEO     fisso = piove; veloce = sicurezza vento
 *       (porta "D6": giallo = PIANTA, bianco = METEO)
 *
 *  Camera pianta: USB sul connettore USB-C (lato Linux), nessun pin MCU.
 * ---------------------------------------------------------------------------
 */

#include <Arduino.h>
#include <Wire.h>
#include <Arduino_RouterBridge.h>

// ======================= CONFIGURAZIONE HARDWARE ===========================

// Metti a 1 quando il servo e' montato. Con 0 l'asse e' simulato: l'angolo
// viene comunque calcolato e riportato in telemetria, ma non esce nessun impulso.
#define HAS_SERVO_AZ     1
#define HAS_SERVO_PITCH  1

// Stacca il segnale quando l'asse e' fermo (meno consumo, niente ronzio).
// Per il pitch lascialo a 0 se il pannello non e' bilanciato sull'asse.
#define AZ_DETACH_IDLE     1
#define PITCH_DETACH_IDLE  0

// Irrigazione: metti a 1 quando il componente e' montato.
#define HAS_VALVE          0   // 0 = sulle uscite ci sono i LED, 1 = ci sono le valvole vere (cambia solo la scritta sulla pagina)
#define HAS_SOIL_SENSOR    0   // senza, il lato Linux stima il fabbisogno d'acqua
#define HAS_TANK_SENSOR    0   // senza, il livello del serbatoio e' stimato
#define VALVE_ACTIVE_HIGH  1   // 0 se l'integrato pilota si attiva a livello basso

// Interruttore di tensione del Grove Base Shield: 1 = su 5V, 0 = su 3.3V.
// A 5 V le linee I2C non usano le resistenze interne dei pin (bastano quelle
// dei moduli), l'AHT20 della pianta sta su D4/D5 e il fotoresistore si legge
// dalla sua uscita digitale.
#define SHIELD_5V  1

// Fotoresistore, modulino a 4 pin (metti HAS_LIGHT_SENSOR a 0 se non e' montato).
// LIGHT_DIGITAL 1: si legge l'uscita DO su D2 (luce si'/no; la soglia e' la vite
//   blu del modulino). E' l'unico modo sicuro con il modulino alimentato a 5 V.
// LIGHT_DIGITAL 0: si legge l'uscita AO su A1 (percentuale). SOLO con il modulino
//   a 3,3 V, oppure a 5 V con un partitore: AO -- 10 k -- A1 -- 20 k -- GND.
#define HAS_LIGHT_SENSOR   1
#define LIGHT_DIGITAL      SHIELD_5V
#define LIGHT_DO_DARK_HIGH 1   // DO alto al buio (come su questi modulini); 0 se e' il contrario
#define LIGHT_INVERT       1   // solo per AO: 1 se la tensione scende con la luce

// Bus dei sensori sull'header SDA/SCL (porte "I2C" del Base Shield):
// 1 = I2C software su D20/D21 (consigliato: funziona anche senza resistenze di
//     pull-up esterne e non puo' bloccare lo sketch se il bus e' scollegato)
// 0 = I2C hardware (Wire): servono pull-up esterni da 4,7 k verso 3V3
#define MAIN_I2C_SOFT  1
const int PIN_MAIN_SDA = D20;
const int PIN_MAIN_SCL = D21;

// Dove sta l'AHT20 della pianta: 1 = bus I2C software su una porta digitale del
// Grove Base Shield, 0 = connettore Qwiic (Wire1, sempre a 3,3 V).
#define PLANT_AHT_ON_SOFT_I2C  1

#if SHIELD_5V
const int PIN_SOFT_SCL = D4;   // porta "D4": filo giallo del cavo Grove
const int PIN_SOFT_SDA = D5;   // filo bianco
#else
const int PIN_SOFT_SCL = D2;   // porta "D2": filo giallo del cavo Grove
const int PIN_SOFT_SDA = D3;   // filo bianco
#endif
const int PIN_SERVO_AZ = D9;
const int PIN_SERVO_PITCH = D10;
const int VALVE_COUNT = 4;
const int PIN_VALVE[VALVE_COUNT] = {A0, A1, A2, A3};   // linee 1..4
const int PIN_TANK = D13;
const int PIN_SOIL = A4;        // umidita' del terreno [STUB]
const int PIN_LIGHT = A1;       // uscita AO (LIGHT_DIGITAL 0)
const int PIN_LIGHT_DO = D2;    // uscita DO (LIGHT_DIGITAL 1)

// LED di stato: metti -1 al posto del pin per quelli che non monti.
#define LED_ACTIVE_HIGH  1
const int PIN_LED_STATUS = D8;    // porta "D8", filo giallo. Battito: 1 lampo ok, 2 lampi sensore mancante, veloce Linux assente
const int PIN_LED_AZ = -1;        // asse di azimut in movimento (reale o simulato)
const int PIN_LED_PITCH = -1;     // asse di pitch in movimento
const int PIN_LED_PLANT = D6;     // fisso luce alla pianta, lampeggiante comando manuale (porta "D6", filo giallo)
const int PIN_LED_WEATHER = D7;   // fisso pioggia, veloce sicurezza vento (porta "D6", filo bianco)
const uint32_t LINK_TIMEOUT_MS = 150000UL;  // senza comandi da Linux per 2,5 min: LED di stato veloce

// Protezioni dell'elettrovalvola, valide anche se il lato Linux sbaglia o si blocca
const uint32_t VALVE_MAX_OPEN_MS = 60000;      // apertura massima di una linea per singolo comando
const uint32_t VALVE_MIN_PAUSE_MS = 300000UL;  // pausa minima tra due aperture

// Taratura del sensore di terreno (lettura a 12 bit): in aria e immerso in acqua
const int SOIL_RAW_DRY = 2900;
const int SOIL_RAW_WET = 1300;

// Come vengono generati gli impulsi dei servo (uno ogni 20 ms):
//   1 = dal timer hardware TIM4, solo su D9 e D10: impulsi precisi, movimento fluido
//   0 = a mano dal loop con digitalWrite: funziona su qualsiasi pin, ma il
//       servo va a scattini (da usare solo se la modalita' 1 non compila)
// La libreria Servo non viene usata: su questo core avvia un interrupt ogni
// 4 microsecondi gia' prima di setup() e bloccava l'avvio dello sketch.
#define SERVO_USE_HW_PWM  1

// Prova visiva all'avvio: il servo di azimut va a 20 gradi, sale a 70 e torna
// a 20, prima che il lato Linux prenda il comando. Metti 0 per disattivarla.
#define SERVO_SELF_TEST  1
const float SELF_TEST_LOW_DEG = 20.0f;
const float SELF_TEST_HIGH_DEG = 70.0f;

// SG90: 50 Hz, circa 500..2400 us per 0..180 gradi
const int SERVO_US_MIN = 500;
const int SERVO_US_MAX = 2400;

// Limiti meccanici in gradi servo (protezione finale, anche se Linux sbaglia)
const float AZ_MIN_DEG = 0.0f, AZ_MAX_DEG = 180.0f;
const float PITCH_MIN_DEG = 0.0f, PITCH_MAX_DEG = 135.0f;   // tarata a mano: 0 = pannello parallelo alla base

// 1 = il servo gira al contrario: l'impulso va a 180 - angolo. Tutto il resto
// (Linux, pagina web, prova all'avvio) continua a ragionare con gli stessi gradi.
#define AZ_SERVO_REVERSED     0
#define PITCH_SERVO_REVERSED  1

const float SERVO_SPEED_DEG_S = 30.0f;      // velocita' di rampa
const uint32_t SERVO_STEP_MS = 20;          // un passo di rampa per periodo servo
const uint32_t SERVO_PERIOD_MS = 20;        // un impulso ogni 20 ms (50 Hz)
const uint32_t SERVO_IDLE_DETACH_MS = 1500; // attesa prima di staccare il segnale

const uint8_t AHT20_ADDR = 0x38;
const uint8_t INA219_ADDR = 0x40;
const float INA219_SHUNT_OHM = 0.1f;        // resistenza di shunt del modulo

const uint32_t AHT_PERIOD_MS = 2000;
const uint32_t INA_PERIOD_MS = 500;
const uint32_t TELEMETRY_PERIOD_MS = 2000;
const uint32_t RETRY_PERIOD_MS = 10000;     // riprova i sensori assenti
const uint32_t COMMS_TIMEOUT_MS = 300000UL; // 5 min senza comandi -> sicurezza

// Bit di stato inviati in telemetria (devono coincidere con python/main.py)
enum StatusBits {
  ST_AHT_AMBIENT = 1 << 0,
  ST_AHT_PLANT   = 1 << 1,
  ST_IMU         = 1 << 2,    // non piu' usato: il Modulino Movement e' stato tolto
  ST_INA         = 1 << 3,
  ST_SERVO_AZ    = 1 << 4,
  ST_SERVO_PITCH = 1 << 5,
  ST_MOVING      = 1 << 6,
  ST_FAILSAFE    = 1 << 7,
  ST_SYNCED      = 1 << 8,
  ST_SOIL        = 1 << 9,    // sensore di terreno presente
  ST_TANK_SENSOR = 1 << 10,   // galleggiante presente
  ST_TANK_OK     = 1 << 11,   // il galleggiante vede acqua
  ST_VALVE_OPEN  = 1 << 12,
  ST_VALVE       = 1 << 13,   // elettrovalvola presente (altrimenti simulata)
  ST_LIGHT       = 1 << 14,   // fotoresistore presente
  ST_LIGHT_DIGITAL = 1 << 15, // il fotoresistore dice solo luce/buio
  ST_VALVE_LINE1 = 1 << 16,   // linea 1 aperta; le linee 2..4 sono i tre bit successivi
};

// Punto raggiunto dallo sketch. Il lato Linux lo legge con la RPC "boot_stage"
// (servita da un thread separato) per capire dove ci si e' fermati se la
// telemetria non arriva. Deve coincidere con BOOT_STAGES in python/tracker.py.
enum BootStage {
  BOOT_START    = 0,   // prima di Bridge.begin()
  BOOT_BRIDGE   = 1,   // Bridge avviato, registrazione dei metodi
  BOOT_SENSORS  = 2,   // inizializzazione dei sensori I2C
  BOOT_IMU_INIT = 3,   // non piu' usato
  BOOT_RUNNING  = 4,   // loop() in esecuzione
  BOOT_IMU_READ = 5,   // non piu' usato
};
volatile int bootStage = BOOT_START;

// Stato deciso dal lato Linux e mostrato dai LED (come in python/tracker.py)
enum LinuxFlags {
  LX_LIGHT  = 1 << 0,   // pannello aperto per la pianta
  LX_MANUAL = 1 << 1,   // comando manuale dalla pagina web
  LX_RAIN   = 1 << 2,   // sta piovendo
  LX_SAFE   = 1 << 3,   // sicurezza vento
};


// ============================== BUS I2C ====================================
// Interfaccia minima comune al bus hardware (Wire, Wire1) e a quello software.

class I2CBus {
public:
  virtual void begin() {}
  virtual bool write(uint8_t addr, const uint8_t *data, size_t n) = 0;
  virtual bool read(uint8_t addr, uint8_t *data, size_t n) = 0;
};

class HardBus : public I2CBus {
public:
  explicit HardBus(HardwareI2C &wire) : _wire(wire) {}

  bool write(uint8_t addr, const uint8_t *data, size_t n) override {
    _wire.beginTransmission(addr);
    _wire.write(data, n);
    return _wire.endTransmission() == 0;
  }

  bool read(uint8_t addr, uint8_t *data, size_t n) override {
    if (_wire.requestFrom(addr, n) != n) return false;
    for (size_t i = 0; i < n; i++) data[i] = (uint8_t)_wire.read();
    return true;
  }

private:
  HardwareI2C &_wire;
};

// I2C "a mano" su due pin qualsiasi, circa 30 kHz. Le linee non vengono mai
// spinte a livello alto: o tirate a massa (uscita bassa) o rilasciate (ingresso
// con pull-up interno), come richiede un bus open-drain.
class SoftBus : public I2CBus {
public:
  SoftBus(int sda, int scl) : _sda(sda), _scl(scl) {}

  void begin() override {
    release(_sda);
    release(_scl);
    wait();
    // Se la scheda si e' riavviata a meta' lettura il sensore puo' tenere SDA
    // bassa: qualche colpo di clock lo sblocca.
    for (uint8_t i = 0; i < 9 && digitalRead(_sda) == LOW; i++) {
      low(_scl); wait();
      release(_scl); wait();
    }
    stop();
  }

  bool write(uint8_t addr, const uint8_t *data, size_t n) override {
    start();
    bool ok = sendByte((uint8_t)(addr << 1));
    for (size_t i = 0; i < n && ok; i++) ok = sendByte(data[i]);
    stop();
    return ok;
  }

  bool read(uint8_t addr, uint8_t *data, size_t n) override {
    start();
    if (!sendByte((uint8_t)((addr << 1) | 1))) {
      stop();
      return false;
    }
    for (size_t i = 0; i < n; i++) data[i] = recvByte(i + 1 < n);
    stop();
    return true;
  }

private:
  // A 5 V la linea e' tenuta alta dalle resistenze del modulo: quella interna
  // del pin va lasciata spenta, altrimenti il pin non sopporta i 5 V.
  void release(int pin) { pinMode(pin, SHIELD_5V ? INPUT : INPUT_PULLUP); }
  void low(int pin) { pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
  void wait() { delayMicroseconds(15); }

  void start() {
    release(_sda); release(_scl); wait();
    low(_sda); wait();
    low(_scl); wait();
  }

  void stop() {
    low(_sda); wait();
    release(_scl); wait();
    release(_sda); wait();
  }

  bool sendByte(uint8_t b) {
    for (uint8_t i = 0; i < 8; i++, b <<= 1) {
      if (b & 0x80) release(_sda); else low(_sda);
      wait();
      release(_scl); wait();
      low(_scl);
    }
    release(_sda); wait();
    release(_scl); wait();
    bool ack = digitalRead(_sda) == LOW;
    low(_scl);
    return ack;
  }

  uint8_t recvByte(bool ack) {
    uint8_t v = 0;
    release(_sda);
    for (uint8_t i = 0; i < 8; i++) {
      wait();
      release(_scl); wait();
      v = (uint8_t)((v << 1) | (digitalRead(_sda) == HIGH ? 1 : 0));
      low(_scl);
    }
    if (ack) low(_sda); else release(_sda);
    wait();
    release(_scl); wait();
    low(_scl);
    release(_sda);
    return v;
  }

  int _sda, _scl;
};

// ============================== AHT20 ======================================
// Driver minimale e non bloccante: il bus si passa nel costruttore, cosi' i
// due sensori (stesso indirizzo) possono stare su due bus diversi.

class Aht20 {
public:
  explicit Aht20(I2CBus &bus, uint32_t phaseMs) : _bus(bus), _phase(phaseMs) {}

  bool begin() {
    _ok = false;
    _state = IDLE;
    uint8_t status;
    if (!readStatus(status)) return false;
    if (!(status & 0x08)) {                 // non calibrato: inizializza
      const uint8_t init[3] = {0xBE, 0x08, 0x00};
      if (!writeBytes(init, 3)) return false;
      delay(10);
    }
    _present = true;
    _errors = 0;
    return true;
  }

  void poll(uint32_t now) {
    if (!_present) {
      if (now - _lastRetry >= RETRY_PERIOD_MS) {
        _lastRetry = now;
        begin();
      }
      return;
    }
    switch (_state) {
      case IDLE:
        if (now - _lastStart >= AHT_PERIOD_MS + _phase) {
          _phase = 0;                       // lo sfasamento serve solo al primo giro
          _lastStart = now;
          const uint8_t trig[3] = {0xAC, 0x33, 0x00};
          if (writeBytes(trig, 3)) {
            _state = WAITING;
            _tries = 0;
          } else {
            fail();
          }
        }
        break;
      case WAITING:
        if (now - _lastStart >= 85u + 20u * _tries) {
          readMeasurement();
        }
        break;
    }
  }

  bool ok() const { return _present && _ok; }
  float temperature() const { return _t; }
  float humidity() const { return _rh; }

private:
  enum State { IDLE, WAITING };

  bool writeBytes(const uint8_t *data, size_t n) {
    return _bus.write(AHT20_ADDR, data, n);
  }

  bool readStatus(uint8_t &status) {
    const uint8_t cmd = 0x71;
    return writeBytes(&cmd, 1) && _bus.read(AHT20_ADDR, &status, 1);
  }

  static uint8_t crc8(const uint8_t *data, size_t n) {
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < n; i++) {
      crc ^= data[i];
      for (uint8_t b = 0; b < 8; b++) {
        crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
      }
    }
    return crc;
  }

  void readMeasurement() {
    uint8_t d[7];
    if (!_bus.read(AHT20_ADDR, d, 7)) {
      fail();
      return;
    }

    if (d[0] & 0x80) {                      // ancora occupato
      if (++_tries > 3) fail();
      return;
    }
    if (crc8(d, 6) != d[6]) {
      fail();
      return;
    }
    uint32_t rawH = ((uint32_t)d[1] << 12) | ((uint32_t)d[2] << 4) | (d[3] >> 4);
    uint32_t rawT = ((uint32_t)(d[3] & 0x0F) << 16) | ((uint32_t)d[4] << 8) | d[5];
    _rh = rawH * 100.0f / 1048576.0f;
    _t = rawT * 200.0f / 1048576.0f - 50.0f;
    _ok = true;
    _errors = 0;
    _state = IDLE;
  }

  void fail() {
    _state = IDLE;
    if (++_errors >= 5) {                   // sensore sparito: riprova piu' tardi
      _present = false;
      _ok = false;
      _lastRetry = millis();
    }
  }

  I2CBus &_bus;
  uint32_t _phase;
  State _state = IDLE;
  bool _present = false, _ok = false;
  uint8_t _tries = 0, _errors = 0;
  uint32_t _lastStart = 0, _lastRetry = 0;
  float _t = 0.0f, _rh = 0.0f;
};

// ============================== INA219 =====================================
// Se il modulo non risponde all'indirizzo 0x40 resta in modalita' stub:
// present() == false e il lato Linux stima la potenza dal modello solare.

class Ina219 {
public:
  explicit Ina219(I2CBus &bus) : _bus(bus) {}

  bool begin() {
    // 32 V, shunt +-320 mV, 12 bit con media di 16 campioni, continuo
    _present = writeReg(0x00, 0x3EEF);
    return _present;
  }

  void poll(uint32_t now) {
    if (now - _last < (_present ? INA_PERIOD_MS : RETRY_PERIOD_MS)) return;
    _last = now;
    if (!_present) {
      begin();
      return;
    }
    uint16_t shunt, bus;
    if (!readReg(0x01, shunt) || !readReg(0x02, bus)) {
      if (++_errors >= 5) _present = false;
      return;
    }
    _errors = 0;
    _busV = (bus >> 3) * 0.004f;                               // LSB 4 mV
    _currentmA = ((int16_t)shunt * 0.01f) / INA219_SHUNT_OHM;  // LSB 10 uV
  }

  bool present() const { return _present; }
  float busVoltage() const { return _present ? _busV : 0.0f; }
  float currentmA() const { return _present ? _currentmA : 0.0f; }

private:
  bool writeReg(uint8_t reg, uint16_t value) {
    const uint8_t data[3] = {reg, (uint8_t)(value >> 8), (uint8_t)(value & 0xFF)};
    return _bus.write(INA219_ADDR, data, 3);
  }

  bool readReg(uint8_t reg, uint16_t &value) {
    uint8_t data[2];
    if (!_bus.write(INA219_ADDR, &reg, 1)) return false;
    if (!_bus.read(INA219_ADDR, data, 2)) return false;
    value = ((uint16_t)data[0] << 8) | data[1];
    return true;
  }

  I2CBus &_bus;
  bool _present = false;
  uint8_t _errors = 0;
  uint32_t _last = 0;
  float _busV = 0.0f, _currentmA = 0.0f;
};

// ============================ ASSE SERVO ===================================

#if SERVO_USE_HW_PWM
#include <zephyr/drivers/pwm.h>
// D9 = TIM4 canale 3, D10 = TIM4 canale 4. I due pin sono gia' collegati al
// timer all'avvio: in questa modalita' non vanno mai toccati con pinMode().
static const struct device *const servoPwmDev = DEVICE_DT_GET(DT_NODELABEL(pwm4));
#endif

class Axis {
public:
  Axis(int pin, int pwmChannel, bool present, bool detachIdle, float minDeg, float maxDeg, bool reversed)
    : _pin(pin), _channel(pwmChannel), _present(present), _detachIdle(detachIdle),
      _min(minDeg), _max(maxDeg), _reversed(reversed) {}

  void begin() {
#if !SERVO_USE_HW_PWM
    if (_present) {
      pinMode(_pin, OUTPUT);
      digitalWrite(_pin, LOW);
    }
#endif
  }

  // Dichiara dove si trova fisicamente l'asse, senza muoverlo.
  void sync(float deg) {
    _cur = _target = constrain(deg, _min, _max);
    _known = true;
  }

  // Ritorna true se il valore richiesto e' stato limitato.
  bool setTarget(float deg) {
    float t = constrain(deg, _min, _max);
    if (!_known) {
      // Posizione ignota (SG90 senza feedback): il primo movimento e' un salto.
      _cur = t;
      _known = true;
      _dirty = true;
    }
    _target = t;
    return t != deg;
  }

  // Rampa verso il bersaglio: da chiamare ogni SERVO_STEP_MS.
  void update(uint32_t now, float maxStepDeg, bool releaseWhenIdle = false) {
    if (!_known) return;
    float err = _target - _cur;
    if (fabsf(err) > 0.05f || _dirty) {
      _cur += constrain(err, -maxStepDeg, maxStepDeg);
      _dirty = false;
      _lastMove = now;
      float deg = _reversed ? 180.0f - _cur : _cur;
      setPulse((int)(SERVO_US_MIN + (deg / 180.0f) * (SERVO_US_MAX - SERVO_US_MIN) + 0.5f));
    } else if (_pulseUs > 0 && (_detachIdle || releaseWhenIdle)
               && now - _lastMove >= (releaseWhenIdle ? 400u : SERVO_IDLE_DETACH_MS)) {
      setPulse(0);                          // asse fermo: niente piu' impulsi
    }
  }

  // Impulsi fatti a mano: da chiamare a ogni giro di loop().
  void refresh(uint32_t now) {
#if !SERVO_USE_HW_PWM
    if (!_present || _pulseUs <= 0 || now - _lastPulse < SERVO_PERIOD_MS) return;
    _lastPulse = now;
    digitalWrite(_pin, HIGH);
    delayMicroseconds((unsigned int)_pulseUs);
    digitalWrite(_pin, LOW);
#else
    (void)now;
#endif
  }

  bool moving() const { return _known && fabsf(_target - _cur) > 0.05f; }
  bool present() const { return _present; }
  bool known() const { return _known; }
  float position() const { return _cur; }
  float target() const { return _target; }
  int pwmError() const { return _pwmError; }

private:
  // us = larghezza dell'impulso; 0 = nessun impulso (servo libero).
  void setPulse(int us) {
    _pulseUs = us;
    if (!_present) return;                  // asse simulato
#if SERVO_USE_HW_PWM
    if (!servoPwmDev->state->initialized || servoPwmDev->state->init_res != 0) {
      _pwmError = -19;                      // il timer TIM4 non e' stato avviato dal sistema
      return;
    }
    _pwmError = pwm_set(servoPwmDev, (uint32_t)_channel, PWM_MSEC(SERVO_PERIOD_MS), PWM_USEC(us), 0);
#endif
  }

  int _pin, _channel;
  bool _present, _detachIdle;
  float _min, _max;
  bool _reversed;
  float _cur = 0.0f, _target = 0.0f;
  bool _known = false, _dirty = false;
  int _pulseUs = 0, _pwmError = 0;
  uint32_t _lastMove = 0, _lastPulse = 0;
};

// ============================== OGGETTI ====================================

#if MAIN_I2C_SOFT
SoftBus busMain(PIN_MAIN_SDA, PIN_MAIN_SCL);   // porte I2C del Base Shield
#else
HardBus busMain(Wire);
#endif
#if PLANT_AHT_ON_SOFT_I2C
SoftBus busPlant(PIN_SOFT_SDA, PIN_SOFT_SCL);
#else
HardBus busPlant(Wire1);         // Qwiic
#endif
Aht20 ahtAmbient(busMain, 0);
Aht20 ahtPlant(busPlant, 1000);  // sfasato di 1 s rispetto all'altro
Ina219 ina(busMain);

Axis axisAz(PIN_SERVO_AZ, 3, HAS_SERVO_AZ, AZ_DETACH_IDLE, AZ_MIN_DEG, AZ_MAX_DEG, AZ_SERVO_REVERSED);
Axis axisPitch(PIN_SERVO_PITCH, 4, HAS_SERVO_PITCH, PITCH_DETACH_IDLE, PITCH_MIN_DEG, PITCH_MAX_DEG, PITCH_SERVO_REVERSED);

bool failsafeSet = false, failsafeActive = false;
float failsafeAz = 0.0f, failsafePitch = 0.0f;
uint32_t lastCommandMs = 0;
bool linkSeen = false;          // il lato Linux ha mandato almeno un comando
int linuxFlags = 0;             // stato deciso da Linux, mostrato dai LED

// Basso consumo: tra un risveglio e l'altro niente impulsi ai servo, niente
// letture dei sensori, LED spenti. Lo decide il lato Linux (RPC set_low_power).
bool lowPower = false;
uint32_t commsTimeoutMs = COMMS_TIMEOUT_MS;

// ============================ RPC ==========================================
// Gli argomenti arrivano come interi in centesimi di grado.
// Sono registrate con provide_safe, quindi girano nel contesto di loop().

// Registrata con provide (non _safe): risponde dal thread del Bridge anche se
// setup() o loop() sono bloccati. Deve restare banale.
int rpcBootStage() {
  return bootStage;
}

int rpcSetTarget(int azCdeg, int pitchCdeg) {
  linkSeen = true;
  bool clamped = axisAz.setTarget(azCdeg / 100.0f);
  clamped |= axisPitch.setTarget(pitchCdeg / 100.0f);
  lastCommandMs = millis();
  failsafeActive = false;
  return clamped ? 1 : 0;
}

// Comunica dove si trovano gli assi senza muoverli (da chiamare all'avvio).
bool rpcSyncPosition(int azCdeg, int pitchCdeg) {
  if (axisAz.moving() || axisPitch.moving()) return false;
  if (!axisAz.known()) axisAz.sync(azCdeg / 100.0f);
  if (!axisPitch.known()) axisPitch.sync(pitchCdeg / 100.0f);
  lastCommandMs = millis();
  return true;
}

// Posa da assumere se il lato Linux smette di mandare comandi.
bool rpcSetFailsafe(int azCdeg, int pitchCdeg) {
  failsafeAz = azCdeg / 100.0f;
  failsafePitch = pitchCdeg / 100.0f;
  failsafeSet = true;
  lastCommandMs = millis();
  return true;
}

void checkFailsafe(uint32_t now) {
  if (!failsafeSet || failsafeActive) return;
  if (now - lastCommandMs < commsTimeoutMs) return;
  failsafeActive = true;
  if (axisAz.known()) axisAz.setTarget(failsafeAz);
  if (axisPitch.known()) axisPitch.setTarget(failsafePitch);
}

// ============================ IRRIGAZIONE ==================================

// Ogni linea ha il suo orario: parte a startMs e resta aperta durationMs.
// Cosi' le linee si possono aprire insieme (comando a mano) oppure una dopo
// l'altra (irrigazione automatica) con lo stesso meccanismo.
struct ValveLine {
  bool scheduled = false;
  bool open = false;
  uint32_t startMs = 0, durationMs = 0;
};
ValveLine valves[VALVE_COUNT];
bool valveUsed = false;
uint32_t valveClosedMs = 0;
float soilRaw = 0.0f;
bool soilInit = false;

void valveWrite(int line, bool open) {
  digitalWrite(PIN_VALVE[line], (open == (VALVE_ACTIVE_HIGH != 0)) ? HIGH : LOW);
  if (valves[line].open && !open) {
    valveClosedMs = millis();
    valveUsed = true;
  }
  valves[line].open = open;
}

void closeAllValves() {
  for (int i = 0; i < VALVE_COUNT; i++) {
    valves[i].scheduled = false;
    valveWrite(i, false);
  }
}

bool anyValveBusy() {
  for (int i = 0; i < VALVE_COUNT; i++) {
    if (valves[i].scheduled) return true;
  }
  return false;
}

int valveOpenMask() {
  int mask = 0;
  for (int i = 0; i < VALVE_COUNT; i++) {
    if (valves[i].open) mask |= 1 << i;
  }
  return mask;
}

bool tankHasWater() {
#if HAS_TANK_SENSOR
  return digitalRead(PIN_TANK) == LOW;
#else
  return true;
#endif
}

float soilPercent() {
  if (!HAS_SOIL_SENSOR || !soilInit) return 0.0f;
  float pct = (SOIL_RAW_DRY - soilRaw) * 100.0f / (float)(SOIL_RAW_DRY - SOIL_RAW_WET);
  return constrain(pct, 0.0f, 100.0f);
}

// Luce misurata dal fotoresistore, 0 (buio) .. 100 (piena luce).
float lightRaw = 0.0f;
bool lightInit = false;

float lightPercent() {
  if (!HAS_LIGHT_SENSOR || !lightInit) return 0.0f;
  float pct = lightRaw * 100.0f / 4095.0f;
#if !LIGHT_DIGITAL
  if (LIGHT_INVERT) pct = 100.0f - pct;
#endif
  return constrain(pct, 0.0f, 100.0f);
}

void pollLight(uint32_t now) {
#if HAS_LIGHT_SENSOR
  static uint32_t last = 0;
  if (now - last < 250 && lightInit) return;
  last = now;
#if LIGHT_DIGITAL
  bool dark = (digitalRead(PIN_LIGHT_DO) == HIGH) == (LIGHT_DO_DARK_HIGH != 0);
  float raw = dark ? 0.0f : 4095.0f;
  lightRaw = lightInit ? lightRaw + 0.5f * (raw - lightRaw) : raw;    // un attimo di filtro contro i tremolii
#else
  float raw = (float)analogRead(PIN_LIGHT);
  lightRaw = lightInit ? lightRaw + 0.3f * (raw - lightRaw) : raw;
#endif
  lightInit = true;
#else
  (void)now;
#endif
}

// Apre le linee indicate dalla maschera (bit 0 = linea 1 ... bit 3 = linea 4)
// per `seconds` secondi ciascuna: una dopo l'altra se `inSequence`, altrimenti
// tutte insieme. Maschera o secondi a zero = chiudi tutto subito.
// Ritorna i secondi concessi a ogni linea, oppure: -1 serbatoio vuoto,
// -2 pausa minima non ancora trascorsa, -3 irrigazione gia' in corso.
int openValves(int mask, int seconds, bool inSequence, bool respectPause) {
  mask &= (1 << VALVE_COUNT) - 1;
  if (mask == 0 || seconds <= 0) {
    closeAllValves();
    return 0;
  }
  if (respectPause && anyValveBusy()) return -3;
  if (!tankHasWater()) return -1;
  if (respectPause && valveUsed && millis() - valveClosedMs < VALVE_MIN_PAUSE_MS) return -2;
  int maxSeconds = (int)(VALVE_MAX_OPEN_MS / 1000);
  if (seconds > maxSeconds) seconds = maxSeconds;
  if (!respectPause) closeAllValves();          // un comando a mano sostituisce quello in corso
  uint32_t start = millis();
  for (int i = 0; i < VALVE_COUNT; i++) {
    if (!(mask & (1 << i))) continue;
    valves[i].scheduled = true;
    valves[i].startMs = start;
    valves[i].durationMs = (uint32_t)seconds * 1000UL;
    if (inSequence) start += valves[i].durationMs;
  }
  return seconds;
}

// Irrigazione automatica: le linee una dopo l'altra, rispettando la pausa minima.
int rpcWater(int mask, int seconds) {
  return openValves(mask, seconds, true, true);
}

// Comando a mano dalla pagina web: le linee scelte tutte insieme, niente pausa
// minima; restano durata massima, controllo del serbatoio e chiusura automatica.
int rpcWaterManual(int mask, int seconds) {
  return openValves(mask, seconds, false, false);
}

void pollIrrigation(uint32_t now) {
  // Apertura e chiusura non dipendono dal lato Linux: ogni linea segue il suo
  // orario, e se finisce l'acqua si chiude tutto.
  if (anyValveBusy() && !tankHasWater()) closeAllValves();
  for (int i = 0; i < VALVE_COUNT; i++) {
    if (!valves[i].scheduled) continue;
    int32_t since = (int32_t)(now - valves[i].startMs);
    if (since < 0) continue;                                 // non e' ancora il suo turno
    if ((uint32_t)since >= valves[i].durationMs) {
      valves[i].scheduled = false;
      valveWrite(i, false);
    } else if (!valves[i].open) {
      valveWrite(i, true);
    }
  }
#if HAS_SOIL_SENSOR
  static uint32_t last = 0;
  if (now - last >= 1000 || !soilInit) {
    last = now;
    float raw = (float)analogRead(PIN_SOIL);
    soilRaw = soilInit ? soilRaw + 0.1f * (raw - soilRaw) : raw;
    soilInit = true;
  }
#endif
}

// ============================ LED ==========================================

// Stato deciso dal lato Linux (bit LinuxFlags), solo per i LED.
// seconds > 0: entra in basso consumo; il lato Linux si rifara' vivo entro
// `seconds`. seconds = 0: sveglia (sensori, telemetria e LED tornano attivi).
bool rpcSetLowPower(int seconds) {
  lowPower = seconds > 0;
  // in basso consumo il silenzio di Linux e' normale: la posa di sicurezza
  // scatta solo se salta due risvegli di fila
  commsTimeoutMs = lowPower ? (uint32_t)seconds * 2000UL + 120000UL : COMMS_TIMEOUT_MS;
  lastCommandMs = millis();
  return lowPower;
}

bool rpcSetStatus(int flags) {
  linuxFlags = flags;
  return true;
}

void ledWrite(int pin, bool on) {
  if (pin < 0) return;
  digitalWrite(pin, (on == (LED_ACTIVE_HIGH != 0)) ? HIGH : LOW);
}

// Tiene acceso il LED di un asse per mezzo secondo dopo l'ultimo movimento,
// altrimenti le piccole correzioni dell'inseguimento (pochi centesimi di
// secondo) non si vedrebbero. Il movimento si riconosce dal cambio di posizione.
struct AxisLed {
  float lastPos = 0.0f;
  uint32_t lastMoveMs = 0;
  bool seen = false;

  bool update(const Axis &axis, uint32_t now) {
    if (axis.known() && axis.position() != lastPos) {
      lastPos = axis.position();
      lastMoveMs = now;
      seen = true;
    }
    return seen && now - lastMoveMs < 500;
  }
};

// Prova dei LED dalla pagina web: accende quelli indicati (bit 0 STATO, 1 AZIMUT,
// 3 PIANTA, 4 METEO, 5 PITCH) e spegne gli altri. Le valvole si provano a parte. Finisce da sola
// dopo 15 secondi, oppure subito con maschera 0.
int ledTestMask = 0;
uint32_t ledTestStartMs = 0;
const uint32_t LED_TEST_MS = 15000;

bool rpcLedTest(int mask) {
  ledTestMask = mask > 0 ? mask : 0;
  ledTestStartMs = millis();
  return ledTestMask != 0;
}

void updateLeds(uint32_t now) {
  static uint32_t last = 0;
  static AxisLed azLed, pitchLed;
  if (now - last < 25) return;
  last = now;

  if (ledTestMask != 0) {
    if (now - ledTestStartMs < LED_TEST_MS) {
      ledWrite(PIN_LED_STATUS, ledTestMask & 1);
      ledWrite(PIN_LED_AZ, ledTestMask & 2);
      ledWrite(PIN_LED_PLANT, ledTestMask & 8);
      ledWrite(PIN_LED_WEATHER, ledTestMask & 16);
      ledWrite(PIN_LED_PITCH, ledTestMask & 32);
      return;
    }
    ledTestMask = 0;
  }

  if (lowPower) {
    // tutto spento, tranne un lampo ogni 10 s (segno di vita) e la valvola
    ledWrite(PIN_LED_STATUS, (now % 10000) < 150);
    ledWrite(PIN_LED_AZ, false);
    ledWrite(PIN_LED_PITCH, false);
    ledWrite(PIN_LED_PLANT, false);
    ledWrite(PIN_LED_WEATHER, false);
    return;
  }

  bool linkAlive = linkSeen && !failsafeActive && now - lastCommandMs < LINK_TIMEOUT_MS;
  bool sensorsOk = ahtAmbient.ok() && ahtPlant.ok();
  uint32_t phase = now % 2000;
  bool status;
  if (!linkAlive) {
    status = (now % 250) < 125;                       // veloce: Linux non comanda
  } else if (sensorsOk) {
    status = phase < 100;                             // un lampo
  } else {
    status = phase < 100 || (phase >= 300 && phase < 400);  // due lampi
  }
  ledWrite(PIN_LED_STATUS, status);
  ledWrite(PIN_LED_AZ, azLed.update(axisAz, now));
  ledWrite(PIN_LED_PITCH, pitchLed.update(axisPitch, now));

  bool slowBlink = (now % 1000) < 500;
  bool fastBlink = (now % 250) < 125;
  bool plant = (linuxFlags & LX_MANUAL) ? slowBlink : (linuxFlags & LX_LIGHT) != 0;
  bool weather = (linuxFlags & LX_SAFE) ? fastBlink : (linuxFlags & LX_RAIN) != 0;
  ledWrite(PIN_LED_PLANT, plant);
  ledWrite(PIN_LED_WEATHER, weather);
}

// ============================ TELEMETRIA ===================================

int statusBits() {
  int s = 0;
  if (ahtAmbient.ok()) s |= ST_AHT_AMBIENT;
  if (ahtPlant.ok()) s |= ST_AHT_PLANT;
  if (ina.present()) s |= ST_INA;
  if (axisAz.present()) s |= ST_SERVO_AZ;
  if (axisPitch.present()) s |= ST_SERVO_PITCH;
  if (axisAz.moving() || axisPitch.moving()) s |= ST_MOVING;
  if (failsafeActive) s |= ST_FAILSAFE;
  if (axisAz.known() && axisPitch.known()) s |= ST_SYNCED;
  if (HAS_SOIL_SENSOR && soilInit) s |= ST_SOIL;
  if (HAS_TANK_SENSOR) s |= ST_TANK_SENSOR;
  if (HAS_TANK_SENSOR && tankHasWater()) s |= ST_TANK_OK;
  int openMask = valveOpenMask();
  if (openMask) s |= ST_VALVE_OPEN;
  s |= openMask * ST_VALVE_LINE1;
  if (HAS_VALVE) s |= ST_VALVE;
  if (HAS_LIGHT_SENSOR && lightInit) s |= ST_LIGHT;
  if (HAS_LIGHT_SENSOR && LIGHT_DIGITAL) s |= ST_LIGHT_DIGITAL;
  return s;
}

void sendTelemetry(uint32_t now) {
  static uint32_t last = 0;
  if (now - last < TELEMETRY_PERIOD_MS) return;
  last = now;
  Bridge.notify("telemetry",
                ahtAmbient.temperature(), ahtAmbient.humidity(),
                ahtPlant.temperature(), ahtPlant.humidity(),
                0.0f, 0.0f,     // inclinazione e scosse: il Modulino Movement non c'e' piu'
                ina.busVoltage(), ina.currentmA(),
                axisAz.position(), axisPitch.position(),
                soilPercent(), statusBits(), lightPercent());
}

// ============================ MESSAGGI =====================================
// I messaggi viaggiano con Bridge.notify, che non aspetta risposta e quindi
// non puo' bloccare lo sketch. Compaiono nella console Python come "[MCU] ...".

void mcuLog(const char *text) {
  Bridge.notify("mcu_log", String(text));
}

// Scrive un float con un decimale senza usare %f, che sul core puo' mancare.
const char *fmt1(char *buf, size_t size, float v) {
  long tenths = (long)(v * 10.0f + (v >= 0.0f ? 0.5f : -0.5f));
  long whole = labs(tenths) / 10;
  snprintf(buf, size, "%s%ld.%ld", tenths < 0 ? "-" : "", whole, labs(tenths) % 10);
  return buf;
}

void printStatus(uint32_t now) {
  static uint32_t last = 0;
  if (now - last < 30000) return;
  last = now;

  char line[160], a[12], b[12], c[12], d[12];
  snprintf(line, sizeof(line), "AHT ambiente %s %sC %s%% | AHT pianta %s %sC %s%%",
           ahtAmbient.ok() ? "ok" : "ASSENTE",
           fmt1(a, sizeof(a), ahtAmbient.temperature()), fmt1(b, sizeof(b), ahtAmbient.humidity()),
           ahtPlant.ok() ? "ok" : "ASSENTE",
           fmt1(c, sizeof(c), ahtPlant.temperature()), fmt1(d, sizeof(d), ahtPlant.humidity()));
  mcuLog(line);

  bool linkAlive = linkSeen && !failsafeActive && now - lastCommandMs < LINK_TIMEOUT_MS;
  snprintf(line, sizeof(line), "servo az %s%s pitch %s%s (%s) | linee aperte (maschera) %s%s | INA %s | luce %s%% | Linux %s",
           fmt1(a, sizeof(a), axisAz.position()), HAS_SERVO_AZ ? "" : " (stub)",
           fmt1(b, sizeof(b), axisPitch.position()), HAS_SERVO_PITCH ? "" : " (stub)",
           SERVO_USE_HW_PWM ? "timer hw" : "impulsi dal loop",
           fmt1(d, sizeof(d), (float)valveOpenMask()), HAS_VALVE ? "" : " (LED)",
           ina.present() ? "ok" : "stub",
           fmt1(c, sizeof(c), lightPercent()),
           linkAlive ? "ok" : "ASSENTE");
  mcuLog(line);

  // Solo con il timer hardware: segnala se il driver PWM ha rifiutato l'impulso.
  if (axisAz.pwmError() != 0 || axisPitch.pwmError() != 0) {
    snprintf(line, sizeof(line), "ERRORE PWM servo: azimut %d, pitch %d", axisAz.pwmError(), axisPitch.pwmError());
    mcuLog(line);
  }
}

// ============================ SETUP / LOOP =================================

// Prova all'avvio: 20 -> 70 -> 20 gradi sul servo di azimut. Il pitch resta
// fermo: la sua corsa e' limitata dal pannello e quei gradi lo farebbero sbattere.
// Si interrompe appena arriva il primo comando dal lato Linux.
void servoSelfTest(uint32_t now) {
#if SERVO_SELF_TEST
  static uint8_t step = 0;
  static uint32_t since = 0;
  if (step >= 3) return;
  if (linkSeen) {
    step = 3;
    return;
  }
  Axis *axes[] = {&axisAz};
  bool moving = axisAz.moving();
  if (step == 0) {
    for (Axis *axis : axes) {
      if (axis->present()) {
        axis->sync(SELF_TEST_LOW_DEG);
        axis->setTarget(SELF_TEST_HIGH_DEG);
      }
    }
    step = 1;
    since = now;
  } else if (!moving && now - since >= 500) {
    if (step == 1) {
      for (Axis *axis : axes) {
        if (axis->present()) axis->setTarget(SELF_TEST_LOW_DEG);
      }
    }
    step++;
    since = now;
  } else if (moving) {
    since = now;
  }
#else
  (void)now;
#endif
}

void updateServos(uint32_t now) {
  static uint32_t last = 0;
  if (now - last < SERVO_STEP_MS) return;
  float step = SERVO_SPEED_DEG_S * (now - last) / 1000.0f;
  last = now;
  axisAz.update(now, step, lowPower);
  axisPitch.update(now, step, lowPower);
}

void setup() {
  // Per prima cosa i pin: valvola chiusa, LED spenti. Niente qui puo' bloccarsi.
  for (int i = 0; i < VALVE_COUNT; i++) {
    pinMode(PIN_VALVE[i], OUTPUT);
    valveWrite(i, false);
  }
#if HAS_TANK_SENSOR
  pinMode(PIN_TANK, INPUT_PULLUP);
#endif
#if HAS_SOIL_SENSOR || (HAS_LIGHT_SENSOR && !LIGHT_DIGITAL)
  analogReadResolution(12);
#endif
#if HAS_LIGHT_SENSOR && LIGHT_DIGITAL
  pinMode(PIN_LIGHT_DO, INPUT);     // senza resistenza interna: la linea puo' stare a 5 V
#endif
  const int ledPins[] = {PIN_LED_STATUS, PIN_LED_AZ, PIN_LED_PITCH, PIN_LED_PLANT, PIN_LED_WEATHER};
  for (int pin : ledPins) {
    if (pin >= 0) {
      pinMode(pin, OUTPUT);
      ledWrite(pin, false);
    }
  }
  // LED di stato acceso fisso finche' loop() non parte: se resta fisso, lo
  // sketch e' fermo in Bridge.begin(); se non si accende mai, non arriva a setup().
  ledWrite(PIN_LED_STATUS, true);
  axisAz.begin();
  axisPitch.begin();

  // Bridge e metodi RPC subito, prima di toccare i sensori: se un sensore
  // dovesse bloccare l'avvio, il lato Linux puo' comunque chiedere dove siamo.
  Bridge.begin();
  bootStage = BOOT_BRIDGE;
  Bridge.provide("boot_stage", rpcBootStage);
  Bridge.provide_safe("set_target", rpcSetTarget);
  Bridge.provide_safe("sync_position", rpcSyncPosition);
  Bridge.provide_safe("set_failsafe", rpcSetFailsafe);
  Bridge.provide_safe("water", rpcWater);
  Bridge.provide_safe("water_manual", rpcWaterManual);
  Bridge.provide_safe("set_status", rpcSetStatus);
  Bridge.provide_safe("set_low_power", rpcSetLowPower);
  Bridge.provide_safe("led_test", rpcLedTest);

  bootStage = BOOT_SENSORS;
#if !MAIN_I2C_SOFT
  Wire.begin();
  Wire.setClock(100000);
#endif
#if !PLANT_AHT_ON_SOFT_I2C
  Wire1.begin();
#endif
  busMain.begin();
  busPlant.begin();
  delay(100);                   // tempo di accensione degli AHT20
  ahtAmbient.begin();
  ahtPlant.begin();
  ina.begin();

  lastCommandMs = millis();
  bootStage = BOOT_RUNNING;
  mcuLog("sketch avviato");
}

void loop() {
  uint32_t now = millis();
  if (!lowPower) {
    ahtAmbient.poll(now);
    ahtPlant.poll(now);
    ina.poll(now);
    pollLight(now);
  }
  servoSelfTest(now);
  axisAz.refresh(now);
  axisPitch.refresh(now);
  updateServos(now);
  checkFailsafe(now);
  pollIrrigation(now);
  updateLeds(now);
  if (!lowPower) {
    printStatus(now);
    sendTelemetry(now);
  }
  // In basso consumo, a servo fermi, il loop gira 10 volte al secondo: il
  // microcontrollore dorme nel frattempo. Altrimenti lascia solo respiro al Bridge.
  bool moving = axisAz.moving() || axisPitch.moving();
  delay((lowPower && !moving) ? 100 : 2);
}
