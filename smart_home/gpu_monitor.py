"""Records GPU (and CPU) temperatures and power while the vision model reads a
receipt, as a baseline for checking cooling (e.g. before and after replacing
thermal paste).

Each scan is a row in gpu_runs with one row per second in gpu_samples. Every
sample is committed as it's taken, so if the machine crashes mid-scan the
readings up to the crash survive and the run is left with no ended_at.

Sensors come from the amdgpu and k10temp hwmon directories in sysfs. While
the GPU is idle it's runtime-suspended and its sensor files return EBUSY;
those readings are stored as NULL.
"""
from __future__ import annotations

import datetime
import glob
import os
import sqlite3
import threading
import time

SAMPLE_SECONDS = 1.0


def _hwmon(name: str) -> str | None:
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().strip() == name:
                    return d
        except OSError:
            continue
    return None


def _read(d: str | None, fname: str, scale: float) -> float | None:
    if d is None:
        return None
    try:
        with open(os.path.join(d, fname)) as f:
            return round(int(f.read().strip()) / scale, 1)
    except (OSError, ValueError):
        return None  # EBUSY while the GPU is asleep, or sensor missing


def sample(gpu: str | None, cpu: str | None) -> dict:
    return {
        "edge": _read(gpu, "temp1_input", 1000),
        "junction": _read(gpu, "temp2_input", 1000),
        "mem": _read(gpu, "temp3_input", 1000),
        "power": _read(gpu, "power1_average", 1_000_000),
        "sclk": _read(gpu, "freq1_input", 1_000_000),
        "fan_rpm": _read(gpu, "fan1_input", 1),
        "cpu": _read(cpu, "temp1_input", 1000),
    }


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Recorder:
    """Context manager: samples once a second on a background thread for as
    long as the block runs. Sensor problems are recorded on the run (its
    error column) rather than interrupting the scan."""

    def __init__(self, db_path: str, receipt_id: int | None, kind: str = "scan"):
        self.db_path = db_path
        self.receipt_id = receipt_id
        self.kind = kind
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.run_id: int | None = None

    def __enter__(self) -> "Recorder":
        gpu, cpu = _hwmon("amdgpu"), _hwmon("k10temp")
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            cur = conn.execute(
                "INSERT INTO gpu_runs (receipt_id, kind, started_at, error) VALUES (?,?,?,?)",
                (self.receipt_id, self.kind, _now(), None if gpu else "No AMD GPU sensors found in /sys/class/hwmon"),
            )
            conn.commit()
            self.run_id = cur.lastrowid
        finally:
            conn.close()
        self._thread = threading.Thread(target=self._loop, args=(gpu, cpu), daemon=True)
        self._thread.start()
        return self

    def _loop(self, gpu: str | None, cpu: str | None) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30)
        start = time.monotonic()
        try:
            while True:
                s = sample(gpu, cpu)
                conn.execute(
                    "INSERT INTO gpu_samples (run_id, t, edge, junction, mem, power, sclk, fan_rpm, cpu) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.run_id, round(time.monotonic() - start, 1), s["edge"], s["junction"], s["mem"],
                     s["power"], s["sclk"], s["fan_rpm"], s["cpu"]),
                )
                conn.commit()
                if self._stop.wait(SAMPLE_SECONDS):
                    break
        except Exception as e:
            print(f"GPU temperature recording failed: {e}", flush=True)
            try:
                conn.execute("UPDATE gpu_runs SET error=? WHERE id=?", (f"Recording stopped: {e}", self.run_id))
                conn.commit()
            except sqlite3.Error:
                pass  # already reported above; the scan itself carries on
        finally:
            conn.close()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("UPDATE gpu_runs SET ended_at=?, outcome=?, error=COALESCE(?, error) WHERE id=?",
                         (_now(), "error" if exc_type else "done", str(exc) if exc else None, self.run_id))
            conn.commit()
        finally:
            conn.close()


_test_lock = threading.Lock()


def test_running() -> bool:
    return _test_lock.locked()


def run_test(db_path: str, receipt_id: int) -> None:
    """Read a receipt's photo again while recording, without changing the
    receipt, so cooling can be compared on an identical workload. Runs in
    the background; a failure is recorded on the run (outcome 'error')."""
    from smart_home import receipts

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT image_path FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not row[0]:
        raise receipts.ReceiptError("That receipt has no photo to read")
    with open(row[0], "rb") as f:
        image = f.read()
    if not _test_lock.acquire(blocking=False):
        raise receipts.ReceiptError("A test scan is already running")

    def _bg():
        try:
            with Recorder(db_path, receipt_id, kind="test"):
                receipts._extract(image)
        except Exception as e:
            print(f"GPU test scan failed: {e}", flush=True)
        finally:
            _test_lock.release()

    threading.Thread(target=_bg, daemon=True).start()
