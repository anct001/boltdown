"""SQLite persistence.

Only coarse state lives here (one row per download, synced every couple of
seconds). Per-segment offsets change far too often for a database and live in
the `.boltdown` sidecar instead - see `app/core/resume.py`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..util.paths import db_path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queues (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    max_concurrent INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    final_url   TEXT,
    filename    TEXT NOT NULL,
    save_path   TEXT NOT NULL,
    size        INTEGER,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL,
    category    TEXT,
    connections INTEGER NOT NULL DEFAULT 8,
    speed_limit INTEGER,
    -- 1 = the file name is settled (user typed it, or a probe already picked
    -- it and a .part file carries it); 0 = let the next probe decide.
    name_locked INTEGER NOT NULL DEFAULT 0,
    queue_id    INTEGER REFERENCES queues(id) ON DELETE SET NULL,
    error       TEXT,
    added_at    REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_downloads_state ON downloads(state);
CREATE INDEX IF NOT EXISTS idx_downloads_queue ON downloads(queue_id);

-- Cookies / referer / auth captured from the browser extension (P3).
CREATE TABLE IF NOT EXISTS download_headers (
    download_id INTEGER PRIMARY KEY REFERENCES downloads(id) ON DELETE CASCADE,
    headers     TEXT NOT NULL DEFAULT '{}',
    cookie      TEXT,
    referer     TEXT,
    user_agent  TEXT,
    proxy       TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id    INTEGER REFERENCES queues(id) ON DELETE CASCADE,
    start_at    TEXT,
    stop_at     TEXT,
    days_mask   INTEGER NOT NULL DEFAULT 127,
    enabled     INTEGER NOT NULL DEFAULT 1,
    on_complete TEXT,
    last_run    REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_queue ON schedules(queue_id);

-- Per-host overrides: connections, speed, headers, proxy.
CREATE TABLE IF NOT EXISTS site_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    connections INTEGER,
    speed_limit INTEGER,
    user_agent  TEXT,
    referer     TEXT,
    cookie      TEXT,
    proxy       TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id INTEGER,
    url         TEXT NOT NULL,
    filename    TEXT,
    size        INTEGER,
    state       TEXT NOT NULL,
    finished_at REAL NOT NULL
);
"""


class Database:
    """Thin synchronous wrapper. Safe to share across threads."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older file up to `SCHEMA_VERSION`.

        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so columns
        added after v1 have to be patched in by hand.
        """
        rows = self.query("SELECT value FROM meta WHERE key = 'schema_version'")
        version = int(rows[0]["value"]) if rows else SCHEMA_VERSION
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            existing = {r["name"] for r in self.query("PRAGMA table_info(schedules)")}
            for column, decl in (("stop_at", "TEXT"), ("last_run", "REAL")):
                if column not in existing:
                    self.execute(f"ALTER TABLE schedules ADD COLUMN {column} {decl}")
        if version < 3:
            existing = {r["name"] for r in self.query("PRAGMA table_info(downloads)")}
            if "name_locked" not in existing:
                self.execute(
                    "ALTER TABLE downloads ADD COLUMN name_locked "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                # Anything already downloading has a .part file named after it.
                self.execute(
                    "UPDATE downloads SET name_locked = 1 WHERE downloaded > 0"
                )
        # v4 only adds a table, which CREATE TABLE IF NOT EXISTS already made.
        self.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )

    # ------------------------------------------------------------------ basics

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --------------------------------------------------------------- downloads

    def add_download(
        self,
        *,
        url: str,
        filename: str,
        save_path: str,
        size: int | None = None,
        state: str = "queued",
        category: str | None = None,
        connections: int = 8,
        speed_limit: int | None = None,
        name_locked: bool = False,
        queue_id: int | None = None,
        headers: dict[str, str] | None = None,
        cookie: str | None = None,
        referer: str | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO downloads
                   (url, filename, save_path, size, state, category, connections,
                    speed_limit, name_locked, queue_id, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, filename, save_path, size, state, category, connections,
                 speed_limit, int(name_locked), queue_id, time.time()),
            )
            download_id = int(cur.lastrowid)
            if any((headers, cookie, referer, user_agent, proxy)):
                self._conn.execute(
                    """INSERT INTO download_headers
                       (download_id, headers, cookie, referer, user_agent, proxy)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (download_id, json.dumps(headers or {}), cookie, referer,
                     user_agent, proxy),
                )
            return download_id

    def update_progress(
        self, download_id: int, downloaded: int, state: str, error: str | None = None
    ) -> None:
        finished = time.time() if state in ("completed", "error", "cancelled") else None
        self.execute(
            """UPDATE downloads
               SET downloaded = ?, state = ?, error = ?,
                   finished_at = COALESCE(?, finished_at)
             WHERE id = ?""",
            (downloaded, state, error, finished, download_id),
        )

    def set_size(self, download_id: int, size: int | None, final_url: str | None) -> None:
        self.execute(
            "UPDATE downloads SET size = ?, final_url = ? WHERE id = ?",
            (size, final_url, download_id),
        )

    def get_download(self, download_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM downloads WHERE id = ?", (download_id,))
        return rows[0] if rows else None

    def list_downloads(self, state: str | None = None) -> list[sqlite3.Row]:
        if state is None:
            return self.query("SELECT * FROM downloads ORDER BY id DESC")
        return self.query(
            "SELECT * FROM downloads WHERE state = ? ORDER BY id DESC", (state,)
        )

    def delete_download(self, download_id: int) -> None:
        self.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

    def archive(self, download_id: int) -> None:
        """Copy a download into the history, once.

        Called both when a download finishes and when the row is removed, so
        the insert has to be idempotent or every finished file would appear
        twice the moment the user cleared it from the list.
        """
        self.execute(
            """INSERT INTO history (download_id, url, filename, size, state, finished_at)
               SELECT id, url, filename, size, state, COALESCE(finished_at, ?)
                 FROM downloads WHERE id = ?
                  AND NOT EXISTS (SELECT 1 FROM history WHERE download_id = ?)""",
            (time.time(), download_id, download_id),
        )

    # ---------------------------------------------------------- site profiles

    def list_profiles(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM site_profiles ORDER BY pattern")

    def save_profile(self, pattern: str, **fields: Any) -> int:
        allowed = ("enabled", "connections", "speed_limit", "user_agent",
                   "referer", "cookie", "proxy", "note")
        values = {k: fields.get(k) for k in allowed}
        values["enabled"] = int(bool(values["enabled"] if values["enabled"] is not None else 1))
        columns = ", ".join(allowed)
        placeholders = ", ".join("?" for _ in allowed)
        updates = ", ".join(f"{name} = excluded.{name}" for name in allowed)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO site_profiles (pattern, {columns}) "
                f"VALUES (?, {placeholders}) "
                f"ON CONFLICT(pattern) DO UPDATE SET {updates}",
                (pattern.strip(), *values.values()),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
        row = self.query("SELECT id FROM site_profiles WHERE pattern = ?", (pattern.strip(),))
        return int(row[0]["id"]) if row else 0

    def delete_profile(self, profile_id: int) -> None:
        self.execute("DELETE FROM site_profiles WHERE id = ?", (profile_id,))

    # ----------------------------------------------------------------- history

    def list_history(self, search: str = "", limit: int = 500) -> list[sqlite3.Row]:
        if search:
            like = f"%{search}%"
            return self.query(
                "SELECT * FROM history WHERE filename LIKE ? OR url LIKE ? "
                "ORDER BY finished_at DESC LIMIT ?",
                (like, like, limit),
            )
        return self.query(
            "SELECT * FROM history ORDER BY finished_at DESC LIMIT ?", (limit,)
        )

    def clear_history(self) -> int:
        cursor = self.execute("DELETE FROM history")
        return cursor.rowcount or 0

    def delete_history(self, history_id: int) -> None:
        self.execute("DELETE FROM history WHERE id = ?", (history_id,))

    # ------------------------------------------------------------------ queues

    def add_queue(self, name: str, *, max_concurrent: int = 1) -> int:
        with self._lock:
            position = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM queues"
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO queues(name, max_concurrent, position) VALUES(?, ?, ?)",
                (name, max(1, max_concurrent), position),
            )
            return int(cur.lastrowid)

    def list_queues(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM queues ORDER BY position, id")

    def get_queue(self, queue_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM queues WHERE id = ?", (queue_id,))
        return rows[0] if rows else None

    def update_queue(self, queue_id: int, **fields: Any) -> None:
        allowed = {"name", "enabled", "max_concurrent", "position"}
        columns = {k: v for k, v in fields.items() if k in allowed}
        if not columns:
            return
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.execute(
            f"UPDATE queues SET {assignments} WHERE id = ?",
            (*columns.values(), queue_id),
        )

    def delete_queue(self, queue_id: int) -> None:
        # Downloads survive their queue (ON DELETE SET NULL); they simply go
        # back to being ordinary independent items.
        self.execute("DELETE FROM queues WHERE id = ?", (queue_id,))

    def set_download_queue(self, download_id: int, queue_id: int | None) -> None:
        self.execute(
            "UPDATE downloads SET queue_id = ? WHERE id = ?", (queue_id, download_id)
        )

    # --------------------------------------------------------------- schedules

    def save_schedule(
        self,
        queue_id: int,
        *,
        start_at: str | None = None,
        stop_at: str | None = None,
        days_mask: int = 127,
        enabled: bool = True,
        on_complete: str | None = None,
        last_run: float | None = None,
    ) -> int:
        """Insert or replace the (single) schedule of a queue."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM schedules WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            values = (start_at, stop_at, days_mask, int(enabled), on_complete, last_run)
            if existing is None:
                cur = self._conn.execute(
                    """INSERT INTO schedules
                       (queue_id, start_at, stop_at, days_mask, enabled, on_complete,
                        last_run)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (queue_id, *values),
                )
                return int(cur.lastrowid)
            self._conn.execute(
                """UPDATE schedules
                      SET start_at = ?, stop_at = ?, days_mask = ?, enabled = ?,
                          on_complete = ?, last_run = ?
                    WHERE id = ?""",
                (*values, existing["id"]),
            )
            return int(existing["id"])

    def list_schedules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM schedules ORDER BY id")

    def get_schedule(self, queue_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM schedules WHERE queue_id = ?", (queue_id,))
        return rows[0] if rows else None

    def mark_schedule_run(self, schedule_id: int, when: float) -> None:
        self.execute("UPDATE schedules SET last_run = ? WHERE id = ?", (when, schedule_id))

    def delete_schedule(self, queue_id: int) -> None:
        self.execute("DELETE FROM schedules WHERE queue_id = ?", (queue_id,))

    # ---------------------------------------------------------------- settings

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default
