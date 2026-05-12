from __future__ import annotations
import json
import multiprocessing
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


def _run_camera_worker(
    name: str,
    base_url: str,
    snapshot_path: str,
    rotation: int,
    initial_zones: list,
    stop_event,
    events_q,
    cmd_q,
    db_path: str,
) -> None:
    """Subprocess target: fetches frames, detects motion, writes process stats."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        events_q.put(("error", "opencv-python-headless not installed"))
        return

    import datetime
    import httpx
    import psutil
    import sqlite3

    db_conn = None
    if db_path:
        try:
            db_conn = sqlite3.connect(db_path, timeout=30)
            db_conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass

    proc = psutil.Process()
    proc.cpu_percent()  # establish baseline; first call always returns 0
    last_stats_t = 0.0

    fgbg = cv2.createBackgroundSubtractorMOG2(
        history=300, varThreshold=40, detectShadows=False
    )
    zones = list(initial_zones)
    zone_streak: dict[str, int] = {}
    warmup_until = time.time() + CameraWatcher.WARMUP_SECONDS

    while not stop_event.is_set():
        # Apply any pending commands from parent (zone or rotation updates)
        while True:
            try:
                cmd = cmd_q.get_nowait()
                if cmd[0] == "update_zones":
                    zones = list(cmd[1])
                    zone_streak.clear()
                elif cmd[0] == "update_rotation":
                    rotation = int(cmd[1])
            except Exception:
                break

        # Write our own process stats every 60 s
        now_t = time.time()
        if now_t - last_stats_t >= 60 and db_conn:
            last_stats_t = now_t
            try:
                cpu = proc.cpu_percent()
                mem = proc.memory_info().rss / 1024 / 1024
                ts = datetime.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                db_conn.execute(
                    "INSERT OR REPLACE INTO camera_process_stats (ts, camera, cpu_percent, mem_mb) VALUES (?,?,?,?)",
                    (ts, name, round(cpu, 1), round(mem, 1)),
                )
                db_conn.commit()
            except Exception:
                pass

        # Fetch a frame
        try:
            r = httpx.get(f"{base_url}{snapshot_path}", timeout=5.0)
            r.raise_for_status()
            buf = np.frombuffer(r.content, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("JPEG decode returned None")
            if rotation and rotation in _ROTATION_CODES:
                frame = cv2.rotate(frame, getattr(cv2, _ROTATION_CODES[rotation]))
        except Exception as e:
            events_q.put(("error", f"Snapshot failed for {name}: {e}"))
            stop_event.wait(CameraWatcher.RECONNECT_WAIT)
            warmup_until = time.time() + CameraWatcher.WARMUP_SECONDS
            zone_streak.clear()
            continue

        if not zones:
            stop_event.wait(CameraWatcher.FRAME_INTERVAL)
            continue

        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fgmask = fgbg.apply(gray)

        contours, _ = cv2.findContours(fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        filtered_mask = np.zeros_like(fgmask)
        for cnt in contours:
            if cv2.contourArea(cnt) >= CameraWatcher.MIN_CONTOUR_PX:
                cv2.drawContours(filtered_mask, [cnt], -1, 255, thickness=cv2.FILLED)

        for zone in zones:
            zname = zone["name"]
            threshold = float(zone.get("sensitivity", 0.05))

            points = zone.get("points")
            if points and len(points) >= 3:
                pts = np.array([[int(px * w), int(py * h)] for px, py in points], dtype=np.int32)
                mask = np.zeros((h, w), dtype=np.uint8)
                cv2.fillPoly(mask, [pts], 255)
                total = int(np.count_nonzero(mask))
                if total == 0:
                    continue
                roi_pixels = int(np.count_nonzero(filtered_mask & mask))
                pct = roi_pixels / total
            else:
                x1 = max(0, int(zone.get("x", 0) * w))
                y1 = max(0, int(zone.get("y", 0) * h))
                x2 = min(w, int((zone.get("x", 0) + zone.get("width", 1)) * w))
                y2 = min(h, int((zone.get("y", 0) + zone.get("height", 1)) * h))
                roi = filtered_mask[y1:y2, x1:x2]
                if roi.size == 0:
                    continue
                pct = float(np.count_nonzero(roi)) / roi.size

            if pct >= threshold:
                zone_streak[zname] = zone_streak.get(zname, 0) + 1
                if zone_streak[zname] == CameraWatcher.STREAK_NEEDED and time.time() >= warmup_until:
                    _, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    events_q.put(("motion", zname, round(pct * 100, 1), enc.tobytes()))
            else:
                zone_streak[zname] = 0

        stop_event.wait(CameraWatcher.FRAME_INTERVAL)

    if db_conn:
        db_conn.close()


class CameraWatcher:
    """Polls an HTTP snapshot endpoint for motion in defined zones.

    Runs in a separate OS process to keep CPU load off the main service process.
    Events placed on self.events (a multiprocessing.Queue):
        ("motion", zone_name, pct_changed, screenshot_bytes)
        ("error",  message)
    """

    STREAK_NEEDED   = 3
    FRAME_INTERVAL  = 0.1
    RECONNECT_WAIT  = 10
    WARMUP_SECONDS  = 30
    MIN_CONTOUR_PX  = 500

    def __init__(self, camera: dict, db_path: str = ""):
        self.name: str = camera["name"]
        self._camera = dict(camera)
        self._db_path = db_path
        self._stop = multiprocessing.Event()
        self.events: multiprocessing.Queue = multiprocessing.Queue()
        self._cmd_q: multiprocessing.Queue = multiprocessing.Queue()
        self._process: multiprocessing.Process | None = None

    def start(self) -> None:
        if self._process and self._process.is_alive():
            return
        self._stop.clear()
        cam = self._camera
        self._process = multiprocessing.Process(
            target=_run_camera_worker,
            args=(
                cam["name"],
                cam["url"].rstrip("/"),
                cam.get("snapshot_path", "/snapshot"),
                int(cam.get("rotation", 0)),
                list(cam.get("zones", [])),
                self._stop,
                self.events,
                self._cmd_q,
                self._db_path,
            ),
            name=f"cam-{self.name}",
            daemon=True,
        )
        self._process.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process:
            self._process.join(timeout=5)

    def update_zones(self, zones: list[dict]) -> None:
        self._camera["zones"] = list(zones)
        self._cmd_q.put(("update_zones", zones))

    def update_rotation(self, degrees: int) -> None:
        self._camera["rotation"] = degrees
        self._cmd_q.put(("update_rotation", degrees))
