"""SQLite connection management and the §18.9 migration gate.

The service owns `deckout.db` outright; nothing else writes it. Migrations are
forward-only and a service expecting a newer schema than the file provides
fails closed.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Bumped whenever schema.sql changes shape. §18.9: startup fails closed when the
# file on disk is older than what the code expects.
EXPECTED_SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SchemaVersionError(RuntimeError):
    """Raised when the database file predates the code's expected schema."""


class Database:
    """A thread-local sqlite3 connection pool over one database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()

    # -- connection ----------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- transactions --------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One local transaction.

        §17.3 rule 5: never hold one of these open across an HTTP call.
        """
        conn = self.conn
        if conn.in_transaction:
            # Nested use joins the caller's transaction rather than opening a
            # second one — SQLite has no real nesting and a silent commit here
            # would break the atomicity the caller is relying on.
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- convenience ---------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # -- migrations ----------------------------------------------------
    def migrate(self) -> int:
        """Apply the schema and record its version. Forward-only."""
        conn = self.conn
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        current = row["version"] if row else 0
        if current > EXPECTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema v{current} is newer than this service "
                f"(v{EXPECTED_SCHEMA_VERSION}); refusing to start"
            )
        if current < EXPECTED_SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
                "applied_at = excluded.applied_at",
                (EXPECTED_SCHEMA_VERSION, utcnow()),
            )
        return EXPECTED_SCHEMA_VERSION
