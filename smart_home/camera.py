from __future__ import annotations
import json
import queue
import threading
import time
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "smart-home"
_CAMERAS_FILE = _CONFIG_DIR / "cameras.json"


def load_config() -> list[dict]:
    if _CAMERAS_FILE.exists():
        try:
            with open(_CAMERAS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def save_config(cameras: list[dict]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CAMERAS_FILE, "w") as f:
        json.dump(cameras, f, indent=2)


def build_rtsp_url(ip: str, username: str, password: str, port: int = 554, subtype: int = 1) -> str:
    """Build a standard Amcrest RTSP URL. subtype=0 is main stream, 1 is sub stream."""
    return f"rtsp://{username}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype={subtype}"


def probe_ports(ip: str, ports: list[int], timeout: float = 2.0) -> list[int]:
    """Return list of TCP ports that are open on ip."""
    import socket
    open_ports = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect((ip, port))
                open_ports.append(port)
            except (ConnectionRefusedError, OSError):
                pass
    return open_ports


def get_snapshot_jpeg(rtsp_url: str) -> tuple[bytes | None, str | None]:
    """Capture a single JPEG frame from the stream.
    Returns (jpeg_bytes, None) on success or (None, error_message) on failure.
    """
    try:
        import cv2
    except ImportError:
        return None, "opencv-python-headless is not installed"
    import os
    # Suppress verbose FFmpeg/OpenCV output — we'll surface errors ourselves
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        return None, "VideoCapture could not open the URL"
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None, "Stream opened but no frame could be read"
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return None, "JPEG encode failed"
    return bytes(buf), None


class CameraWatcher:
    """Watches an RTSP stream for motion in defined zones.

    Runs a background thread that pulls frames at ~10 fps, applies a MOG2
    background subtractor, and emits motion events when changed pixels in a
    zone exceed the zone's sensitivity threshold for STREAK_NEEDED consecutive
    frames (debouncing single-frame noise).

    Events placed on self.events:
        ("motion", zone_name, pct_changed)
        ("error",  message)
    """

    STREAK_NEEDED = 3   # consecutive frames with motion before firing
    FRAME_SLEEP   = 0.1 # seconds between frame reads (~10 fps)
    RECONNECT_WAIT = 10  # seconds before reconnecting after stream failure

    def __init__(self, camera: dict):
        self.name: str = camera["name"]
        self.rtsp_url: str = camera["rtsp_url"]
        self.zones: list[dict] = list(camera.get("zones", []))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.events: queue.Queue = queue.Queue()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"cam-{self.name}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update_zones(self, zones: list[dict]) -> None:
        """Hot-reload zones without restarting the thread."""
        self.zones = list(zones)

    def _run(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.events.put(("error", "opencv-python-headless not installed"))
            return

        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                self.events.put(("error", f"Could not open stream for {self.name}"))
                self._stop.wait(self.RECONNECT_WAIT)
                continue

            fgbg = cv2.createBackgroundSubtractorMOG2(
                history=300, varThreshold=40, detectShadows=False
            )
            zone_streak: dict[str, int] = {}

            while not self._stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    break  # stream dropped — reconnect

                zones = self.zones
                if not zones:
                    time.sleep(0.5)
                    continue

                h, w = frame.shape[:2]
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                fgmask = fgbg.apply(gray)

                for zone in zones:
                    name = zone["name"]
                    x1 = max(0, int(zone["x"] * w))
                    y1 = max(0, int(zone["y"] * h))
                    x2 = min(w, int((zone["x"] + zone["width"]) * w))
                    y2 = min(h, int((zone["y"] + zone["height"]) * h))
                    roi = fgmask[y1:y2, x1:x2]
                    if roi.size == 0:
                        continue
                    pct = float(np.count_nonzero(roi)) / roi.size
                    threshold = float(zone.get("sensitivity", 0.05))
                    if pct >= threshold:
                        zone_streak[name] = zone_streak.get(name, 0) + 1
                        if zone_streak[name] == self.STREAK_NEEDED:
                            self.events.put(("motion", name, round(pct * 100, 1)))
                    else:
                        zone_streak[name] = 0

                time.sleep(self.FRAME_SLEEP)

            cap.release()
            if not self._stop.is_set():
                self._stop.wait(self.RECONNECT_WAIT)
