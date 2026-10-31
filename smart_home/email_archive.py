"""A local copy of the whole Gmail mailbox, kept on tank2 and shown on /email-archive.

Every message is saved exactly as Gmail has it (format=raw) as an .eml file
under <data dir>/email-archive/YYYY/MM/<gmail id>.eml, which any mail program
can open. index.db in the same folder lists every message with its headers
and labels, and has a full-text index of subject, sender, recipients and body
for the page's search. It's kept out of readings.db so the archive is one
self-contained folder.

The first sync lists the whole mailbox (spam and trash left out), newest
first, 500 messages a page, and saves the page it got to so a restart carries
on where it stopped. After that, each sync asks Gmail's history for what
changed since the last one: new messages are saved, label changes are
recorded, and a message deleted from Gmail keeps its local copy with
deleted_at set. Gmail only keeps about a week of history; if the archive falls
further behind than that, the mailbox is listed again, which only fetches
what's missing.

A message that can't be fetched or saved is kept in `failed` and tried again
at the start of each sync, up to MAX_ATTEMPTS times.

The monitor starts a sync every SYNC_INTERVAL. A lock file stops the monitor
and the web server's "Sync now" from running two at once. It uses the same
read-only Gmail access as the emailed bills (`smart-home gmail-setup`).
"""
from __future__ import annotations

import base64
import datetime
import fcntl
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import Message
from email.parser import BytesParser

import httpx

from smart_home import gmail

SYNC_INTERVAL = datetime.timedelta(minutes=15)
# Messages fetched at once. Gmail allows 250 quota units a second per user and
# a raw message costs 5, so this stays well under it.
WORKERS = 8
MAX_ATTEMPTS = 5
# Longest body text kept in the search index
MAX_BODY = 100_000
HISTORY_TYPES = ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]


class ArchiveError(Exception):
    pass


class _HTTPError(ArchiveError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def archive_dir(db_path: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "email-archive")
    os.makedirs(d, exist_ok=True)
    return d


def open_index(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(archive_dir(db_path), "index.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # labels is a JSON list of label names. path is relative to archive_dir.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY,
            message_id  TEXT NOT NULL UNIQUE,
            thread_id   TEXT,
            received_at TEXT NOT NULL,
            sender      TEXT NOT NULL,
            recipients  TEXT NOT NULL,
            subject     TEXT NOT NULL,
            snippet     TEXT,
            labels      TEXT NOT NULL,
            size        INTEGER NOT NULL,
            path        TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            deleted_at  TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS messages_received ON messages (received_at)")
    # rowid is messages.id
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(subject, sender, recipients, body)")
    # history_id: where the next sync's history starts (unset until the first full listing finishes).
    # listing_history_id / listing_page: a full listing in progress. gmail_total: messagesTotal from Gmail.
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    # kind is 'full' (listing the mailbox) or 'update' (history since the last sync)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at   TEXT,
            ok         INTEGER,
            error      TEXT,
            kind       TEXT,
            n_new      INTEGER NOT NULL DEFAULT 0,
            n_failed   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed (
            message_id TEXT PRIMARY KEY,
            error      TEXT NOT NULL,
            attempts   INTEGER NOT NULL,
            last_tried TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM state WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


# ---- Reading a saved message ---------------------------------------------------

def _header(msg: Message, name: str) -> str:
    try:
        return str(msg.get(name, "") or "")
    except Exception:
        # A header the parser can't decode: keep it as it was sent
        return next((str(v) for k, v in msg.raw_items() if k.lower() == name.lower()), "")


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:  # a charset Python doesn't know
        return payload.decode("utf-8", errors="replace")


def parse(raw: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(raw)


def body_text(msg: Message) -> str:
    """The plain-text body, or the visible text of the HTML one."""
    plain = html = None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ct = part.get_content_type()
        if ct == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ct == "text/html" and html is None:
            html = _part_text(part)
    if plain and plain.strip():
        return plain.strip()
    if html:
        p = gmail._HTMLText()
        p.feed(html)
        return p.text()
    return ""


def attachments(msg: Message) -> list[dict]:
    """Attachments as {index, filename, content_type, size}; index is the part's
    position in msg.walk(), used to fetch it again."""
    out = []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != "attachment":
            continue
        out.append({"index": i, "filename": filename or f"attachment-{i}",
                    "content_type": part.get_content_type(),
                    "size": len(part.get_payload(decode=True) or b"")})
    return out


def attachment(msg: Message, index: int) -> tuple[str, str, bytes] | None:
    for i, part in enumerate(msg.walk()):
        if i == index and not part.is_multipart():
            return (part.get_filename() or f"attachment-{i}", part.get_content_type(),
                    part.get_payload(decode=True) or b"")
    return None


# ---- Syncing -------------------------------------------------------------------

class _Sync:
    def __init__(self, cfg: dict, client: httpx.Client, conn: sqlite3.Connection, root: str,
                 run_id: int, progress):
        self.cfg, self.client, self.conn, self.root = cfg, client, conn, root
        self.run_id, self.progress = run_id, progress
        self.n_new = self.n_failed = 0
        self.labels: dict[str, str] = {}

    def get(self, path: str, params: dict | None = None) -> dict:
        """A Gmail API call, retried with backoff when Gmail is busy or unreachable."""
        for attempt in range(6):
            last = attempt == 5
            try:
                resp = self.client.get(f"{gmail.API_BASE}{path}", params=params,
                                       headers={"Authorization": f"Bearer {gmail._access_token(self.cfg)}"})
            except httpx.HTTPError as e:
                if last:
                    raise ArchiveError(f"Could not reach Gmail: {e}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return resp.json()
            busy = resp.status_code in (429, 500, 502, 503, 504) or (
                resp.status_code == 403 and "rateLimitExceeded" in resp.text)
            if resp.status_code == 401 and attempt == 0:
                gmail._access["token"] = None  # expired early; get a new one
                continue
            if busy and not last:
                time.sleep(2 ** attempt)
                continue
            try:
                msg = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = resp.text[:200]
            raise _HTTPError(resp.status_code, f"Gmail API {path} failed (HTTP {resp.status_code}): {msg}")

    def _report(self) -> None:
        self.conn.execute("UPDATE runs SET n_new=?, n_failed=? WHERE id=?", (self.n_new, self.n_failed, self.run_id))
        self.conn.commit()
        if self.progress:
            self.progress(self.n_new, self.n_failed)

    def _fetch(self, mid: str) -> tuple[str, dict | None, str | None]:
        try:
            return mid, self.get(f"/messages/{mid}", {"format": "raw"}), None
        except _HTTPError as e:
            if e.status == 404:
                return mid, None, None  # deleted from Gmail before it was fetched
            return mid, None, str(e)

    def _fail(self, mid: str, error: str) -> None:
        self.n_failed += 1
        self.conn.execute(
            "INSERT INTO failed (message_id, error, attempts, last_tried) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET error=excluded.error, attempts=attempts + 1, "
            "last_tried=excluded.last_tried", (mid, error, _now()))

    def _store(self, m: dict) -> None:
        raw = base64.urlsafe_b64decode(m["raw"] + "=" * (-len(m["raw"]) % 4))
        received = datetime.datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000)
        rel = os.path.join(received.strftime("%Y"), received.strftime("%m"), f"{m['id']}.eml")
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(path + ".tmp", path)
        msg = parse(raw)
        sender = _header(msg, "From")
        recipients = ", ".join(h for h in (_header(msg, "To"), _header(msg, "Cc")) if h)
        subject = _header(msg, "Subject")
        cur = self.conn.execute(
            "INSERT INTO messages (message_id, thread_id, received_at, sender, recipients, subject, snippet, "
            "labels, size, path, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO NOTHING",
            (m["id"], m.get("threadId"), received.strftime("%Y-%m-%d %H:%M:%S"), sender, recipients, subject,
             m.get("snippet"), self._label_names(m.get("labelIds")), len(raw), rel, _now()))
        if cur.rowcount:
            self.conn.execute("INSERT INTO messages_fts (rowid, subject, sender, recipients, body) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (cur.lastrowid, subject, sender, recipients, body_text(msg)[:MAX_BODY]))
            self.n_new += 1
        self.conn.execute("DELETE FROM failed WHERE message_id=?", (m["id"],))

    def _label_names(self, ids: list[str] | None) -> str:
        return json.dumps([self.labels.get(i, i) for i in ids or []])

    def fetch_missing(self, ids: list[str]) -> None:
        if not ids:
            return
        have = {r[0] for r in self.conn.execute(
            f"SELECT message_id FROM messages WHERE message_id IN ({', '.join('?' * len(ids))})", ids)}
        todo = [i for i in ids if i not in have]
        with ThreadPoolExecutor(WORKERS) as pool:
            # Network and sign-in errors are raised here and end the sync; an
            # error about one message is recorded for it and the rest carry on
            for mid, m, err in pool.map(self._fetch, todo):
                if err:
                    self._fail(mid, err)
                elif m:
                    try:
                        self._store(m)
                    except Exception as e:
                        self._fail(mid, f"Could not save: {type(e).__name__}: {e}")
        self.conn.commit()
        self._report()

    def retry_failed(self) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT message_id FROM failed WHERE attempts < ?", (MAX_ATTEMPTS,))]
        for i in range(0, len(ids), 500):
            self.fetch_missing(ids[i:i + 500])

    def full(self) -> None:
        """List the whole mailbox and fetch every message that isn't saved yet."""
        self.conn.execute("UPDATE runs SET kind='full' WHERE id=?", (self.run_id,))
        if not _get_state(self.conn, "listing_history_id"):
            # Anything that changes while listing is picked up by the first update
            _set_state(self.conn, "listing_history_id", self.get("/profile")["historyId"])
        page = _get_state(self.conn, "listing_page")
        while True:
            params = {"maxResults": 500}
            if page:
                params["pageToken"] = page
            try:
                body = self.get("/messages", params)
            except _HTTPError as e:
                if e.status == 400 and page:  # the saved page has expired: start the listing over
                    page = None
                    continue
                raise
            self.fetch_missing([m["id"] for m in body.get("messages", [])])
            page = body.get("nextPageToken")
            _set_state(self.conn, "listing_page", page)
            if not page:
                break
        _set_state(self.conn, "history_id", _get_state(self.conn, "listing_history_id"))
        _set_state(self.conn, "listing_history_id", None)

    def update(self) -> None:
        """Fetch what Gmail's history says changed since the last sync."""
        self.conn.execute("UPDATE runs SET kind='update' WHERE id=?", (self.run_id,))
        start = latest = _get_state(self.conn, "history_id")
        added: dict[str, None] = {}
        page = None
        while True:
            params = {"startHistoryId": start, "maxResults": 500, "historyTypes": HISTORY_TYPES}
            if page:
                params["pageToken"] = page
            try:
                body = self.get("/history", params)
            except _HTTPError as e:
                if e.status == 404:  # further behind than Gmail keeps history
                    _set_state(self.conn, "history_id", None)
                    self.full()
                    return
                raise
            for h in body.get("history", []):
                for a in h.get("messagesAdded", []):
                    if "SPAM" not in (a["message"].get("labelIds") or []):
                        added[a["message"]["id"]] = None
                for d in h.get("messagesDeleted", []):
                    self.conn.execute("UPDATE messages SET deleted_at=? WHERE message_id=? AND deleted_at IS NULL",
                                      (_now(), d["message"]["id"]))
                for lc in h.get("labelsAdded", []) + h.get("labelsRemoved", []):
                    self.conn.execute("UPDATE messages SET labels=? WHERE message_id=?",
                                      (self._label_names(lc["message"].get("labelIds")), lc["message"]["id"]))
            latest = body.get("historyId", latest)
            page = body.get("nextPageToken")
            if not page:
                break
        self.conn.commit()
        ids = list(added)
        for i in range(0, len(ids), 500):
            self.fetch_missing(ids[i:i + 500])
        _set_state(self.conn, "history_id", latest)


def _lock(db_path: str):
    """The sync lock file, held; None if another process has it."""
    f = open(os.path.join(archive_dir(db_path), ".sync.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def is_syncing(db_path: str) -> bool:
    f = _lock(db_path)
    if f is None:
        return True
    f.close()
    return False


def sync(db_path: str, progress=None) -> dict:
    """Bring the archive up to date. progress(n_new, n_failed) is called after
    each batch. Recorded in runs; raises ArchiveError if it can't finish."""
    cfg = gmail.load_config()
    if not cfg:
        raise ArchiveError("Gmail isn't connected. Run `smart-home gmail-setup` on tank2.")
    lock = _lock(db_path)
    if lock is None:
        raise ArchiveError("A sync is already running")
    conn = open_index(db_path)
    try:
        # Only one sync runs at a time, so an unfinished one was cut off
        conn.execute("UPDATE runs SET ended_at=?, ok=0, error='Interrupted (the service was restarted?)' "
                     "WHERE ended_at IS NULL", (_now(),))
        run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),)).lastrowid
        conn.commit()
        s = None
        try:
            with httpx.Client(timeout=60) as client:
                s = _Sync(cfg, client, conn, archive_dir(db_path), run_id, progress)
                s.labels = {lb["id"]: lb["name"] for lb in s.get("/labels").get("labels", [])}
                _set_state(conn, "gmail_total", s.get("/profile").get("messagesTotal"))
                s.retry_failed()
                if _get_state(conn, "history_id"):
                    s.update()
                else:
                    s.full()
        except Exception as e:
            msg = str(e) if isinstance(e, (ArchiveError, gmail.GmailError)) else f"{type(e).__name__}: {e}"
            conn.rollback()
            conn.execute("UPDATE runs SET ended_at=?, ok=0, error=?, n_new=?, n_failed=? WHERE id=?",
                         (_now(), msg, s.n_new if s else 0, s.n_failed if s else 0, run_id))
            conn.commit()
            raise ArchiveError(msg) from e
        conn.execute("UPDATE runs SET ended_at=?, ok=1, error=?, n_new=?, n_failed=? WHERE id=?",
                     (_now(), f"{s.n_failed} message(s) couldn't be saved" if s.n_failed else None,
                      s.n_new, s.n_failed, run_id))
        conn.commit()
        return {"n_new": s.n_new, "n_failed": s.n_failed}
    finally:
        conn.close()
        lock.close()


def sync_due(db_path: str) -> bool:
    if not gmail.CONFIG_PATH.exists() or is_syncing(db_path):
        return False
    conn = open_index(db_path)
    try:
        row = conn.execute("SELECT ended_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return True  # never synced, or the last one was cut off
    return datetime.datetime.now() - datetime.datetime.fromisoformat(row[0]) >= SYNC_INTERVAL


def sync_in_background(db_path: str) -> None:
    def _bg():
        try:
            r = sync(db_path)
            if r["n_new"] or r["n_failed"]:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] Email archive: {r['n_new']} new, {r['n_failed']} failed", flush=True)
        except Exception as e:
            print(f"Email archive sync failed: {e}", flush=True)  # also in runs, shown on /email-archive

    threading.Thread(target=_bg, daemon=True).start()


def status(db_path: str) -> dict:
    conn = open_index(db_path)
    try:
        n, size, n_deleted, oldest, newest = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), COUNT(deleted_at), MIN(received_at), MAX(received_at) "
            "FROM messages").fetchone()
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10")]
        failed = [dict(r) for r in conn.execute("SELECT * FROM failed ORDER BY last_tried DESC LIMIT 20")]
        n_failed = conn.execute("SELECT COUNT(*) FROM failed").fetchone()[0]
        gmail_total = _get_state(conn, "gmail_total")
        return {
            "connected": gmail.CONFIG_PATH.exists(),
            "syncing": is_syncing(db_path),
            "full_listing_done": _get_state(conn, "history_id") is not None,
            "n_messages": n, "size": size, "n_deleted": n_deleted, "oldest": oldest, "newest": newest,
            "gmail_total": int(gmail_total) if gmail_total else None,
            "runs": runs, "failed": failed, "n_failed": n_failed, "max_attempts": MAX_ATTEMPTS,
            "interval_minutes": int(SYNC_INTERVAL.total_seconds() // 60),
            "path": archive_dir(db_path),
        }
    finally:
        conn.close()


def _fts_query(q: str) -> str:
    """Each word as a prefix match, so search text can't be read as FTS syntax."""
    return " ".join('"' + w.replace('"', '""') + '"*' for w in q.split())


def search(db_path: str, q: str, offset: int = 0, limit: int = 50) -> dict:
    conn = open_index(db_path)
    try:
        cols = "m.message_id, m.received_at, m.sender, m.recipients, m.subject, m.snippet, m.labels, m.size, m.deleted_at"
        if q.strip():
            fq = _fts_query(q)
            total = conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", (fq,)).fetchone()[0]
            rows = conn.execute(
                f"SELECT {cols} FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                f"WHERE messages_fts MATCH ? ORDER BY m.received_at DESC LIMIT ? OFFSET ?", (fq, limit, offset)).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            rows = conn.execute(f"SELECT {cols} FROM messages m ORDER BY m.received_at DESC LIMIT ? OFFSET ?",
                                (limit, offset)).fetchall()
        return {"total": total, "offset": offset, "limit": limit,
                "messages": [dict(r, labels=json.loads(r["labels"])) for r in rows]}
    finally:
        conn.close()


def get_message(db_path: str, message_id: str) -> tuple[dict, bytes] | None:
    """(index row, raw .eml bytes), or None if it isn't in the archive."""
    conn = open_index(db_path)
    try:
        row = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    path = os.path.join(archive_dir(db_path), row["path"])
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ArchiveError(f"The saved copy at {path} can't be read: {e}")
    return dict(row, labels=json.loads(row["labels"])), raw
