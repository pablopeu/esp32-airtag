"""SQLite storage: tags, their Find My keys, and decrypted location reports."""

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """One connection per thread (WAL allows concurrent readers)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(os.path.join(config.DATA_DIR, "tracker.db"), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    _conn().executescript(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS keys (
            id          INTEGER PRIMARY KEY,
            tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            key_index   INTEGER NOT NULL,
            private_key BLOB NOT NULL,
            UNIQUE (tag_id, key_index)
        );
        CREATE TABLE IF NOT EXISTS reports (
            tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            key_index   INTEGER NOT NULL,
            ts          TEXT NOT NULL,
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            accuracy    REAL,
            confidence  INTEGER,
            UNIQUE (tag_id, key_index, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_reports_tag_ts ON reports(tag_id, ts);
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        """
    )
    _conn().commit()


# --- meta helpers -----------------------------------------------------------


def meta_get(key: str) -> str | None:
    row = _conn().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


def meta_set(key: str, value: str) -> None:
    _conn().execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    _conn().commit()


# --- tags -------------------------------------------------------------------


def create_tag(name: str, private_keys: list[bytes]) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO tags (name, created_at) VALUES (?, ?)",
        (name, datetime.now(timezone.utc).isoformat()),
    )
    tag_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO keys (tag_id, key_index, private_key) VALUES (?, ?, ?)",
        [(tag_id, i, key) for i, key in enumerate(private_keys)],
    )
    conn.commit()
    return tag_id


def delete_tag(tag_id: int) -> None:
    conn = _conn()
    conn.execute("DELETE FROM reports WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM keys WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()


def list_tags() -> list[dict]:
    rows = _conn().execute(
        """
        SELECT t.id, t.name, t.created_at,
               (SELECT COUNT(*) FROM keys k WHERE k.tag_id = t.id) AS key_count,
               (SELECT MAX(r.ts) FROM reports r WHERE r.tag_id = t.id) AS last_ts,
               (SELECT r.lat FROM reports r WHERE r.tag_id = t.id
                 ORDER BY r.ts DESC LIMIT 1) AS last_lat,
               (SELECT r.lon FROM reports r WHERE r.tag_id = t.id
                 ORDER BY r.ts DESC LIMIT 1) AS last_lon,
               (SELECT r.accuracy FROM reports r WHERE r.tag_id = t.id
                 ORDER BY r.ts DESC LIMIT 1) AS last_accuracy
        FROM tags t ORDER BY t.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_tag(tag_id: int) -> dict | None:
    row = _conn().execute(
        "SELECT id, name, created_at FROM tags WHERE id = ?", (tag_id,)
    ).fetchone()
    return dict(row) if row else None


def tag_private_keys(tag_id: int) -> list[tuple[int, bytes]]:
    rows = _conn().execute(
        "SELECT key_index, private_key FROM keys WHERE tag_id = ? ORDER BY key_index",
        (tag_id,),
    ).fetchall()
    return [(r["key_index"], bytes(r["private_key"])) for r in rows]


# --- reports ----------------------------------------------------------------


def insert_reports(tag_id: int, reports: list[dict]) -> int:
    """Insert decrypted reports; returns how many were new."""
    conn = _conn()
    inserted = 0
    for r in reports:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO reports
                (tag_id, key_index, ts, lat, lon, accuracy, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tag_id,
                r["key_index"],
                r["ts"],
                r["lat"],
                r["lon"],
                r.get("accuracy"),
                r.get("confidence"),
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def reports_for_tag(tag_id: int, hours: float = 24.0) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = _conn().execute(
        """
        SELECT key_index, ts, lat, lon, accuracy, confidence
        FROM reports WHERE tag_id = ? AND ts >= ? ORDER BY ts
        """,
        (tag_id, since),
    ).fetchall()
    return [dict(r) for r in rows]


def prune_old_reports() -> None:
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    conn = _conn()
    conn.execute("DELETE FROM reports WHERE ts < ?", (cutoff_iso,))
    conn.commit()
