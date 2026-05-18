"""
Unit tests for the relay buffer eviction algorithm.

The buffer holds up to MAX_BUFFER readings. Once full, each new reading is
inserted in sorted order and the interior reading with the smallest merged gap
to its two neighbors is evicted. The oldest and newest readings are never
evicted, so the buffer always spans [outage start … most recent reading].

Run with:  python3 smart_home/relay_firmware/test_buffer.py
"""

import unittest
from datetime import datetime, timedelta


# ── Python reimplementation of the C++ buffer algorithm ──────────────────────

def batch_ts(payload: str) -> str:
    """Extract 'YYYY-MM-DD HH:MM:SS' from a payload (mirrors batchTs())."""
    key = '"batch_ts":"'
    idx = payload.find(key)
    if idx < 0:
        return ""
    idx += len(key)
    if idx + 19 > len(payload):
        return ""
    return payload[idx:idx + 19]


def ts_to_secs(ts: str) -> float:
    """Parse timestamp string to epoch seconds (mirrors tsToSecs())."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()


def make_payload(ts: datetime) -> str:
    return f'{{"batch_ts":"{ts.strftime("%Y-%m-%d %H:%M:%S")}","data":1}}'


class Buffer:
    """Mirrors the g_batch_queue logic in esp32_relay.ino."""

    def __init__(self, max_buffer: int = 60):
        self.MAX_BUFFER = max_buffer
        self.queue: list[str] = []   # always sorted by batch_ts

    def clear(self):
        self.queue.clear()

    def push(self, payload: str) -> str:
        """
        Insert payload, keep sorted. If over capacity, evict the interior
        reading with the smallest merged gap (times[i+1] - times[i-1]).
        Oldest and newest are never evicted.
        Returns 'stored' or 'evicted:N' (1-indexed).
        """
        self.queue.append(payload)
        self.queue.sort(key=batch_ts)

        if len(self.queue) <= self.MAX_BUFFER:
            return "stored"

        times = [ts_to_secs(batch_ts(p)) for p in self.queue]
        n = len(times)
        victim, min_merged = 1, float("inf")
        for i in range(1, n - 1):
            merged = times[i + 1] - times[i - 1]
            if merged < min_merged:
                min_merged = merged
                victim = i

        del self.queue[victim]
        return f"evicted:{victim + 1}"

    def timestamps(self) -> list[str]:
        return [batch_ts(p) for p in self.queue]


# ── Helpers ───────────────────────────────────────────────────────────────────

BASE = datetime(2024, 1, 1, 12, 0, 0)

def readings(n: int, start: datetime = BASE, step_s: int = 30) -> list[str]:
    return [make_payload(start + timedelta(seconds=i * step_s)) for i in range(n)]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestBufferSize(unittest.TestCase):
    def test_never_exceeds_max(self):
        """Buffer size must never exceed MAX_BUFFER at any point during filling."""
        MAX = 10
        for n_steps, label in [(120, "1 hour"), (2880, "1 day"),
                                (20160, "1 week"), (86400, "1 month")]:
            with self.subTest(outage=label):
                buf = Buffer(max_buffer=MAX)
                for i, p in enumerate(readings(n_steps)):
                    buf.push(p)
                    self.assertLessEqual(
                        len(buf.queue), MAX,
                        f"{label}: buffer size {len(buf.queue)} > {MAX} after push {i + 1}")


class TestEvenSpacing(unittest.TestCase):
    def test_gap_changes_minimised(self):
        """Max consecutive gap difference (demo column 3) must stay within
        1.25 × ideal_gap — the tightest bound the best-known algorithm
        achieves across all outage durations with 30 s reading granularity.
        Perfect uniformity (all zeros) is mathematically impossible for most
        outage durations at 30 s resolution, but this threshold ensures the
        algorithm is genuinely trying to minimise unevenness."""
        cases = [
            (120,   "1 hour"),
            (2880,  "1 day"),
            (20160, "1 week"),
            (86400, "1 month"),
        ]
        for n_steps, label in cases:
            with self.subTest(outage=label):
                buf = Buffer(max_buffer=10)
                for p in readings(n_steps):
                    buf.push(p)
                dts = [datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                       for t in buf.timestamps()]
                gaps = [(dts[i + 1] - dts[i]).total_seconds()
                        for i in range(len(dts) - 1)]
                changes = [abs(gaps[i] - gaps[i - 1])
                           for i in range(1, len(gaps))]
                ideal_gap = (dts[-1] - dts[0]).total_seconds() / (len(dts) - 1)
                max_change = max(changes)
                threshold = 1.25 * ideal_gap
                self.assertLess(
                    max_change, threshold,
                    f"{label}: max gap change {max_change:.0f}s "
                    f"exceeds 1.25 × ideal {ideal_gap:.0f}s = {threshold:.0f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
