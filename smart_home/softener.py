"""Water softener salt: when salt was added, how often it needs adding, and a
push reminder when it's due.

Each time salt goes in the tank, a row is added to softener_salt_additions
with the day, and how many days late (or early, negative) it was by the
user's judgement — a tank found empty was late. The ideal interval between
refills is learned from the gaps between additions, each corrected by the
later one's days_late: 200 days apart but 30 late means 170 is the interval.
The estimate is the mean of the last INTERVALS_USED corrected gaps.

The monitor calls check_reminder() every hour; once the next refill is due it
sends a push at REMINDER_HOUR or later, and again every REPEAT_DAYS until salt
is added.
"""
from __future__ import annotations
import datetime
import sqlite3

INTERVALS_USED = 3
REMINDER_HOUR = 9
REPEAT_DAYS = 7


def _day(s: str) -> datetime.date:
    return datetime.date.fromisoformat(s[:10])


def additions(conn: sqlite3.Connection) -> list[dict]:
    """Every addition, oldest first, each with the gap since the one before
    (gap_days) and that gap corrected for lateness (ideal_days)."""
    rows = conn.execute(
        "SELECT id, day, days_late, note, created_at FROM softener_salt_additions ORDER BY day, id"
    ).fetchall()
    out, prev = [], None
    for r in rows:
        a = dict(r)
        if prev:
            a['gap_days'] = (_day(a['day']) - _day(prev['day'])).days
            a['ideal_days'] = a['gap_days'] - a['days_late']
        else:
            a['gap_days'] = a['ideal_days'] = None
        out.append(a)
        prev = a
    return out


def purchases(conn: sqlite3.Connection) -> list[dict]:
    """Softener salt bought, from store receipts (the line's category)."""
    day = "COALESCE(r.purchase_date, substr(r.uploaded_at, 1, 10))"
    rows = conn.execute(
        f"SELECT r.id AS receipt_id, {day} AS day, r.merchant, i.description, i.quantity, i.total_price "
        f"FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id "
        f"WHERE r.status='done' AND i.category='Water Softener Salt' ORDER BY day, r.id"
    ).fetchall()
    return [dict(r) for r in rows]


def status(conn: sqlite3.Connection, today: datetime.date | None = None) -> dict:
    """The learned interval, when salt is next due and how far off that is.
    interval_days and next_due are None until there are two additions."""
    today = today or datetime.date.today()
    adds = additions(conn)
    ideals = [a['ideal_days'] for a in adds if a['ideal_days'] is not None and a['ideal_days'] > 0]
    used = ideals[-INTERVALS_USED:]
    interval = round(sum(used) / len(used)) if used else None
    last = adds[-1] if adds else None
    next_due = None
    if last and interval:
        next_due = _day(last['day']) + datetime.timedelta(days=interval)
    last_reminder = None
    if last:
        row = conn.execute(
            "SELECT MAX(sent_at) FROM softener_reminders WHERE addition_id=?", (last['id'],)
        ).fetchone()
        last_reminder = row[0]
    return {
        'additions': adds,
        'interval_days': interval,
        'n_intervals': len(used),
        'last_day': last['day'] if last else None,
        'days_since': (today - _day(last['day'])).days if last else None,
        'next_due': next_due.isoformat() if next_due else None,
        'days_until_due': (next_due - today).days if next_due else None,
        'last_reminder': last_reminder,
    }


def add(conn: sqlite3.Connection, day: str, days_late: int = 0, note: str | None = None) -> int:
    datetime.date.fromisoformat(day)  # ValueError on a bad date
    cur = conn.execute(
        "INSERT INTO softener_salt_additions (day, days_late, note, created_at) VALUES (?,?,?,?)",
        (day, int(days_late), note or None, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return cur.lastrowid


def check_reminder(conn: sqlite3.Connection, now: datetime.datetime | None = None) -> str | None:
    """Send the refill push if it's due and not sent in the last REPEAT_DAYS.
    Returns what happened (for the monitor's log), or None if nothing was due."""
    from smart_home import push as _push
    now = now or datetime.datetime.now()
    if now.hour < REMINDER_HOUR:
        return None
    st = status(conn, now.date())
    if st['days_until_due'] is None or st['days_until_due'] > 0:
        return None
    if st['last_reminder']:
        since = now - datetime.datetime.fromisoformat(st['last_reminder'])
        if since < datetime.timedelta(days=REPEAT_DAYS):
            return None
    overdue = -st['days_until_due']
    when = "today" if overdue == 0 else f"{overdue} day{'s' if overdue != 1 else ''} ago"
    body = (f"Time to add salt to the water softener (due {when}). "
            f"Last added {st['days_since']} days ago; it usually lasts about {st['interval_days']} days.")
    if not _push.send_notification(title="Water Softener Salt", body=body):
        return "reminder due but the push failed"
    conn.execute(
        "INSERT INTO softener_reminders (addition_id, sent_at) VALUES (?,?)",
        (st['additions'][-1]['id'], now.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return f"reminder sent ({when})"
