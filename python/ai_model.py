"""Interfaccia verso i modelli AI.

Il resto dell'app usa solo le due funzioni di `ModelHub`:

    classify_plant(image)   -> PlantResult    che pianta e'
    assess_health(image)    -> HealthResult   come sta la pianta

Per collegare i modelli veri basta aggiungere in questa cartella un file
`colleague_model.py` con una funzione `create()` che ritorna un oggetto con
uno o entrambi questi metodi (quello mancante resta simulato):

    classify_plant(image)   image = foto come matrice numpy RGB (alto, largo, 3)
    assess_health(image)    stessa foto

Ogni metodo puo' ritornare l'oggetto risultato (PlantResult, HealthResult),
oppure piu' semplicemente una coppia (etichetta, confidenza da 0 a 1).
I modelli veri di questo progetto sono caricati da colleague_model.py.
Per la salute, se il modello non dice esplicitamente se la pianta e' sana o se
ha sete, lo si deduce dall'etichetta con le parole chiave in config.py.
"""

import logging
from dataclasses import dataclass

import config

log = logging.getLogger("ai")


@dataclass
class PlantResult:
    label: str
    confidence: float       # 0..1
    simulated: bool = False
    group: str | None = None            # genere botanico, se il modello lo sa dare
    group_confidence: float = 0.0
    alternatives: tuple = ()            # altre ipotesi: (etichetta, confidenza)

    def best_label(self, minimum: float) -> str | None:
        """La specie se e' abbastanza sicura, altrimenti il genere, altrimenti niente."""
        if self.confidence >= minimum:
            return self.label
        if self.group and self.group_confidence >= minimum:
            return self.group
        return None


@dataclass
class HealthResult:
    label: str              # etichetta del modello, es. "sana", "foglie secche"
    confidence: float       # 0..1
    healthy: bool = True
    needs_water: bool = False
    simulated: bool = False


def _has_keyword(label: str, keywords) -> bool:
    text = (label or "").lower()
    return any(k in text for k in keywords)


def _as_plant(result, simulated: bool) -> PlantResult:
    if isinstance(result, PlantResult):
        return result
    if isinstance(result, dict):
        return PlantResult(str(result.get("label", "sconosciuta")), float(result.get("confidence", 0.0)), simulated,
                           result.get("group"), float(result.get("group_confidence", 0.0)),
                           tuple((str(n), float(c)) for n, c in result.get("alternatives", ())))
    label, confidence = result
    return PlantResult(str(label), float(confidence), simulated)


def _as_health(result, simulated: bool) -> HealthResult:
    if isinstance(result, HealthResult):
        return result
    if isinstance(result, dict):
        label, confidence = str(result.get("label", "sconosciuto")), float(result.get("confidence", 0.0))
        healthy, needs_water = result.get("healthy"), result.get("needs_water")
    else:
        label, confidence = str(result[0]), float(result[1])
        healthy = needs_water = None
    if needs_water is None:
        needs_water = _has_keyword(label, config.HEALTH_THIRSTY_KEYWORDS)
    if healthy is None:
        healthy = _has_keyword(label, config.HEALTH_OK_KEYWORDS) and not needs_water
    return HealthResult(label, confidence, bool(healthy), bool(needs_water), simulated)


class StubAI:
    """Sostituto dei modelli: regole semplici, risultati marcati come simulati."""

    def classify_plant(self, image) -> PlantResult:
        if image is None:
            return PlantResult("sconosciuta", 0.0, simulated=True)
        return PlantResult(config.SIM_PLANT_LABEL, 0.9, simulated=True)

    def assess_health(self, image) -> HealthResult:
        """Finto controllo di salute: guarda solo se nella foto domina il verde."""
        try:
            red, green, blue = (float(image[..., i].mean()) for i in range(3))
        except Exception:
            return HealthResult("non valutabile", 0.0, healthy=True, simulated=True)
        if green > red * 1.08 and green > blue:
            return HealthResult("sana", 0.8, healthy=True, simulated=True)
        if red >= green:
            return HealthResult("foglie secche (stress idrico)", 0.7, healthy=False, needs_water=True, simulated=True)
        return HealthResult("da controllare", 0.5, healthy=False, simulated=True)


class ModelHub:
    """Usa i modelli veri dove ci sono e quelli simulati per il resto."""

    def __init__(self, real=None):
        self._real = real
        self._stub = StubAI()

    def _pick(self, name: str):
        fn = getattr(self._real, name, None)
        return (fn, False) if callable(fn) else (getattr(self._stub, name), True)

    def is_real(self, name: str) -> bool:
        return callable(getattr(self._real, name, None))

    def classify_plant(self, image) -> PlantResult:
        fn, simulated = self._pick("classify_plant")
        return _as_plant(fn(image), simulated)

    def assess_health(self, image) -> HealthResult:
        fn, simulated = self._pick("assess_health")
        return _as_health(fn(image), simulated)


def load_model() -> ModelHub:
    try:
        import colleague_model  # type: ignore

        real = colleague_model.create()
    except Exception as exc:
        log.warning("modelli veri non disponibili (%s): tutti i modelli sono simulati", exc)
        return ModelHub()
    hub = ModelHub(real)
    for name, what in (("classify_plant", "tipo di pianta"), ("assess_health", "salute della pianta")):
        log.info("modello %s: %s", what, "REALE" if hub.is_real(name) else "simulato")
    return hub
