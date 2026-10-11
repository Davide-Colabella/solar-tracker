"""Modelli veri (file ONNX nella cartella python/models/).

Quali file usare e il significato delle loro uscite stanno in config.py
(sezione "Modelli AI"). Un modello che manca, o che non si riesce a caricare,
resta simulato: l'app parte lo stesso e la pagina web lo segnala.

Per eseguire i modelli serve `onnxruntime` (python/requirements.txt); se non
c'e' si prova con OpenCV (cv2.dnn).
"""

import logging
import os

import config

log = logging.getLogger("modelli")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def _load_runner(path: str):
    """Ritorna (funzione che esegue il modello su un tensore 1x3xHxW, nome del motore)."""
    try:
        import onnxruntime as ort
    except ImportError:
        import cv2

        net = cv2.dnn.readNetFromONNX(path)

        def run_cv(x):
            net.setInput(x)
            return net.forward()

        return run_cv, "OpenCV"
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    return (lambda x: session.run(None, {name: x})[0]), "onnxruntime"


def _resize_rgb(image, short_side: int, crop: int):
    """Foto qualsiasi -> matrice RGB crop x crop (lato corto a short_side, poi ritaglio centrale)."""
    import numpy as np

    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    arr = arr[..., :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    h, w = arr.shape[:2]
    k = short_side / float(min(h, w))
    nw, nh = max(crop, int(round(w * k))), max(crop, int(round(h * k)))
    try:
        from PIL import Image

        arr = np.asarray(Image.fromarray(arr).resize((nw, nh), Image.BILINEAR))
    except ImportError:
        try:
            import cv2

            arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        except ImportError:                 # senza librerie: campionamento semplice
            rows = (np.arange(nh) * h / nh).astype(int)
            cols = (np.arange(nw) * w / nw).astype(int)
            arr = arr[rows][:, cols]
    top, left = (nh - crop) // 2, (nw - crop) // 2
    return arr[top:top + crop, left:left + crop]


class OnnxClassifier:
    """Classificatore di immagini.

    Ritorna etichetta e confidenza (0..1). Le uscite con la stessa etichetta
    vengono sommate. Con `by_genus` somma anche le specie dello stesso genere
    (prima parola dell'etichetta) e aggiunge le alternative piu' probabili:
    serve ai modelli con molte specie simili, dove il genere e' piu' sicuro
    della specie.
    """

    def __init__(self, path: str, labels, by_genus: bool = False):
        self.labels = tuple(labels)
        self.by_genus = by_genus
        self._run, self.engine = _load_runner(path)

    def __call__(self, image):
        import numpy as np

        rgb = _resize_rgb(image, config.MODEL_RESIZE, config.MODEL_INPUT_SIZE).astype(np.float32) / 255.0
        rgb = (rgb - np.array(config.MODEL_MEAN, np.float32)) / np.array(config.MODEL_STD, np.float32)
        x = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32)
        logits = np.asarray(self._run(x), dtype=np.float64).reshape(-1)
        if len(logits) != len(self.labels):
            raise ValueError(f"il modello ha {len(logits)} uscite ma le etichette sono {len(self.labels)}")
        p = np.exp(logits - logits.max())
        p /= p.sum()
        by_label: dict[str, float] = {}
        for label, prob in zip(self.labels, p):
            by_label[label] = by_label.get(label, 0.0) + float(prob)
        ranked = sorted(by_label.items(), key=lambda kv: -kv[1])
        label, confidence = ranked[0]
        if not self.by_genus:
            return label, confidence
        by_genus: dict[str, float] = {}
        for name, prob in by_label.items():
            genus = name.split(" ")[0]
            by_genus[genus] = by_genus.get(genus, 0.0) + prob
        genus = label.split(" ")[0]
        return {"label": label, "confidence": confidence, "group": genus, "group_confidence": by_genus[genus],
                "alternatives": [(name, prob) for name, prob in ranked[1:4] if prob >= 0.02]}


class Models:
    pass


def _labels(labels):
    """Etichette scritte in config.py, oppure nome di un file con un'etichetta per riga."""
    if isinstance(labels, str):
        with open(os.path.join(MODEL_DIR, labels), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return list(labels)


def _try_load(what: str, filename, labels, by_genus: bool = False):
    if not filename:
        return None
    path = os.path.join(MODEL_DIR, filename)
    try:
        names = _labels(labels)
        model = OnnxClassifier(path, names, by_genus)
        shown = names if len(names) <= 6 else f"{len(names)} etichette"
        log.info("modello %s caricato: %s (%s), uscite: %s", what, filename, model.engine, shown)
        return model
    except Exception as exc:
        log.warning("modello %s (%s) non caricato, resta simulato: %s", what, filename, exc)
        return None


def create():
    models = Models()
    health = _try_load("salute della pianta", config.HEALTH_MODEL_FILE, config.HEALTH_MODEL_LABELS)
    if health is not None:
        models.assess_health = health
    plant = _try_load("tipo di pianta", config.PLANT_MODEL_FILE, config.PLANT_MODEL_LABELS, by_genus=True)
    if plant is not None:
        models.classify_plant = plant
    return models
