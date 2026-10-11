"""Pagina web dell'impianto (brick WebUI di App Lab, porta 7000).

    GET  /state   stato corrente, letto dalla pagina una volta al secondo
    POST /photo   foto caricata a mano: passa dai due modelli (tipo di pianta, salute)
    POST /manual  {"azimuth": gradi bussola, "tilt": gradi}: muove il pannello a mano
    POST /valve   {"seconds": n, "lines": maschera}: apre le linee scelte per n secondi (0 = chiude tutto)
    POST /light   {"point": "dark" | "bright" | "reset"}: taratura del fotoresistore
    POST /debug   {"action": "start" | "stop" | "servo" | "leds" | "stage" | "camera", ...}: prova dei componenti
    POST /auto    torna al controllo automatico

La pagina e' in assets/index.html.
"""

import base64
import io
import logging

log = logging.getLogger("web")


def decode_image(data: str):
    """Da 'data:image/jpeg;base64,...' a matrice numpy RGB (alto, largo, 3)."""
    raw = base64.b64decode(data.split(",", 1)[-1])
    try:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw)) as photo:
            return np.asarray(ImageOps.exif_transpose(photo).convert("RGB"))
    except ImportError:
        import cv2
        import numpy as np

        bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("immagine non leggibile")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def start(tracker):
    """Avvia la pagina web. Ritorna l'oggetto WebUI, o None se il brick non c'e'."""
    try:
        from arduino.app_bricks.web_ui import WebUI
        from fastapi import Body
    except ImportError as exc:
        log.warning("interfaccia web non disponibile (%s): aggiungi il brick WebUI all'app", exc)
        return None

    ui = WebUI()

    def get_state():
        return tracker.snapshot()

    def post_photo(payload: dict = Body(...)):
        try:
            image = decode_image(str(payload.get("image", "")))
        except Exception as exc:
            log.warning("foto non leggibile: %s", exc)
            return {"error": f"Foto non leggibile: {exc}"}
        try:
            return tracker.analyze_photo(image)
        except Exception as exc:
            log.exception("analisi della foto non riuscita")
            return {"error": f"Analisi non riuscita: {exc}"}

    def post_manual(payload: dict = Body(...)):
        try:
            return tracker.set_manual(float(payload["azimuth"]), float(payload["tilt"]))
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"Comando non valido: {exc}"}

    def post_auto():
        return tracker.set_auto()

    def post_valve(payload: dict = Body(...)):
        try:
            lines = payload.get("lines")
            return tracker.water_now(float(payload["seconds"]), None if lines is None else int(lines))
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"Comando non valido: {exc}"}

    def post_debug(payload: dict = Body(...)):
        try:
            args = {k: v for k, v in payload.items() if k != "action"}
            return tracker.debug_command(str(payload.get("action", "")), **args)
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"Comando non valido: {exc}"}

    def post_light(payload: dict = Body(...)):
        return tracker.calibrate_light(str(payload.get("point", "")))

    for method, path, handler in (("GET", "/state", get_state), ("POST", "/photo", post_photo),
                                  ("POST", "/manual", post_manual), ("POST", "/auto", post_auto),
                                  ("POST", "/valve", post_valve), ("POST", "/light", post_light),
                                  ("POST", "/debug", post_debug)):
        if hasattr(ui, "expose_api"):
            ui.expose_api(method, path, handler)
        else:                                   # versioni meno recenti del brick
            ui.app.add_api_route(path, handler, methods=[method])
    log.info("interfaccia web pronta sulla porta 7000")
    return ui
