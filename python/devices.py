"""Periferiche lato Linux: camera della pianta."""

import logging

log = logging.getLogger("periferiche")


class PlantCamera:
    """Camera sotto il pannello, tramite la periferica Camera di App Lab."""

    def _open(self):
        try:
            from arduino.app_peripherals.camera import Camera
        except ImportError:
            # versioni meno recenti di App Lab
            from arduino.app_peripherals.usb_camera import USBCamera
            return USBCamera()
        cam = Camera(self._source) if self._source is not None else Camera()
        cam.start()
        return cam

    def __init__(self, source=None):
        self._source = source
        self._camera = None
        self._warned = False

    def capture(self):
        """Ritorna un fotogramma numpy, oppure None se la camera non c'e'."""
        try:
            if self._camera is None:
                self._camera = self._open()
            return self._camera.capture()
        except Exception as exc:
            if not self._warned:
                log.warning("camera della pianta non disponibile: %s", exc)
                self._warned = True
            self._camera = None
            return None
