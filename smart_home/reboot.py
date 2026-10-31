"""Notify when the server has come back from a reboot.

The kernel's boot_id changes on every boot but not when the service restarts, so
comparing it with the last one seen tells a reboot apart from a redeploy.
"""
from __future__ import annotations
import datetime
import sqlite3
import threading
import time
from pathlib import Path

import psutil

from smart_home import push as _push

_BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
_STATE_FILE = Path.home() / ".config" / "smart-home" / "last_boot_id"
# Right after boot the network (or APNs) may not be reachable yet, so keep retrying.
_RETRY_S = 30
_GIVE_UP_S = 60 * 60


def _fmt_time(t: datetime.datetime) -> str:
    return t.strftime("%-I:%M %p")


def _fmt_duration(td: datetime.timedelta) -> str:
    m = int(td.total_seconds() // 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60}h {m % 60}m"


def _message(hostname: str, db_path: str | None) -> str:
    booted = datetime.datetime.fromtimestamp(psutil.boot_time())
    body = f"{hostname} is back up after rebooting at {_fmt_time(booted)}."
    if not db_path:
        return body
    # The last sensor reading before boot is roughly when the server went down.
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            row = conn.execute("SELECT MAX(ts) FROM readings WHERE ts < ?",
                               (booted.strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()
    except sqlite3.Error as e:
        return f"{body} (Couldn't work out the downtime: {e})"
    if row and row[0]:
        last = datetime.datetime.fromisoformat(row[0])
        body += f" It was down for about {_fmt_duration(booted - last)} (last reading at {_fmt_time(last)})."
    return body


def _notify(boot_id: str, hostname: str, db_path: str | None) -> None:
    body = _message(hostname, db_path)
    deadline = time.monotonic() + _GIVE_UP_S
    while not _push.send_notification(title=f"{hostname} rebooted", body=body):
        if time.monotonic() > deadline:
            print(f"[reboot] gave up sending the reboot notification after {_GIVE_UP_S // 60} min")
            break
        print(f"[reboot] reboot notification not delivered, retrying in {_RETRY_S}s")
        time.sleep(_RETRY_S)
    _STATE_FILE.write_text(boot_id)


def check_for_reboot(db_path: str | None) -> None:
    """Send a push in the background if this is the first run since the machine booted."""
    import socket
    try:
        boot_id = _BOOT_ID_FILE.read_text().strip()
    except OSError as e:
        print(f"[reboot] can't read boot id, reboot notifications disabled: {e}")
        return
    last = _STATE_FILE.read_text().strip() if _STATE_FILE.exists() else None
    if last == boot_id:
        return  # service restart, not a reboot
    if last is None:
        # First run ever: nothing to compare against yet.
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(boot_id)
        return
    print("[reboot] first run since boot, sending notification")
    threading.Thread(target=_notify, args=(boot_id, socket.gethostname(), db_path),
                     name="reboot-notify", daemon=True).start()
