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


_ROTATION_CODES = {
    90:  "ROTATE_90_CLOCKWISE",
    180: "ROTATE_180",
    270: "ROTATE_90_COUNTERCLOCKWISE",
}

def rotate_jpeg(jpeg_bytes: bytes, degrees: int) -> bytes:
    """Rotate a JPEG by 0/90/180/270 degrees. Returns original bytes if degrees==0."""
    if degrees == 0 or degrees not in _ROTATION_CODES:
        return jpeg_bytes
    import cv2
    import numpy as np
    buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    rotated = cv2.rotate(img, getattr(cv2, _ROTATION_CODES[degrees]))
    _, enc = cv2.imencode('.jpg', rotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return enc.tobytes()


def get_snapshot_jpeg(base_url: str, snapshot_path: str = "/snapshot") -> tuple[bytes | None, str | None]:
    """Fetch a JPEG from GET <base_url><snapshot_path>.

    Returns (jpeg_bytes, None) on success or (None, error_message) on failure.
    """
    import httpx
    try:
        r = httpx.get(f"{base_url.rstrip('/')}{snapshot_path}", timeout=5.0)
        r.raise_for_status()
        return r.content, None
    except Exception as e:
        return None, str(e)


class CameraWatcher:
    """Polls an HTTP snapshot endpoint for motion in defined zones.

    Fetches JPEG frames at ~10 fps from <url>/snapshot, applies a MOG2
    background subtractor, and emits motion events when changed pixels in a
    zone exceed the zone's sensitivity threshold for STREAK_NEEDED consecutive
    frames.

    Events placed on self.events:
        ("motion", zone_name, pct_changed)
        ("error",  message)
    """

    STREAK_NEEDED   = 3    # consecutive frames with motion before firing
    FRAME_INTERVAL  = 0.1  # seconds between snapshot requests (~10 fps)
    RECONNECT_WAIT  = 10   # seconds to wait after a fetch failure

    def __init__(self, camera: dict):
        self.name: str = camera["name"]
        self.base_url: str = camera["url"].rstrip("/")
        self.snapshot_path: str = camera.get("snapshot_path", "/snapshot")
        self.rotation: int = int(camera.get("rotation", 0))
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

    def update_rotation(self, degrees: int) -> None:
        """Hot-reload rotation without restarting the thread."""
        self.rotation = degrees

    def _run(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.events.put(("error", "opencv-python-headless not installed"))
            return

        import httpx

        fgbg = cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=40, detectShadows=False
        )
        zone_streak: dict[str, int] = {}

        while not self._stop.is_set():
            # Fetch a frame
            try:
                r = httpx.get(f"{self.base_url}{self.snapshot_path}", timeout=5.0)
                r.raise_for_status()
                buf = np.frombuffer(r.content, dtype=np.uint8)
                frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError("JPEG decode returned None")
                if self.rotation and self.rotation in _ROTATION_CODES:
                    frame = cv2.rotate(frame, getattr(cv2, _ROTATION_CODES[self.rotation]))
            except Exception as e:
                self.events.put(("error", f"Snapshot failed for {self.name}: {e}"))
                self._stop.wait(self.RECONNECT_WAIT)
                continue

            zones = self.zones
            if not zones:
                self._stop.wait(self.FRAME_INTERVAL)
                continue

            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            fgmask = fgbg.apply(gray)

            for zone in zones:
                zname = zone["name"]
                threshold = float(zone.get("sensitivity", 0.05))

                # Build a polygon mask from normalized points
                points = zone.get("points")
                if points and len(points) >= 3:
                    pts = np.array([[int(px * w), int(py * h)] for px, py in points], dtype=np.int32)
                    mask = np.zeros((h, w), dtype=np.uint8)
                    cv2.fillPoly(mask, [pts], 255)
                    total = int(np.count_nonzero(mask))
                    if total == 0:
                        continue
                    roi_pixels = int(np.count_nonzero(fgmask & mask))
                    pct = roi_pixels / total
                else:
                    # Legacy rectangle support
                    x1 = max(0, int(zone.get("x", 0) * w))
                    y1 = max(0, int(zone.get("y", 0) * h))
                    x2 = min(w, int((zone.get("x", 0) + zone.get("width", 1)) * w))
                    y2 = min(h, int((zone.get("y", 0) + zone.get("height", 1)) * h))
                    roi = fgmask[y1:y2, x1:x2]
                    if roi.size == 0:
                        continue
                    pct = float(np.count_nonzero(roi)) / roi.size

                if pct >= threshold:
                    zone_streak[zname] = zone_streak.get(zname, 0) + 1
                    if zone_streak[zname] == self.STREAK_NEEDED:
                        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        self.events.put(("motion", zname, round(pct * 100, 1), buf.tobytes()))
                else:
                    zone_streak[zname] = 0

            self._stop.wait(self.FRAME_INTERVAL)
