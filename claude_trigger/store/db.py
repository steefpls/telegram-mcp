import hashlib
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("telegram_mcp.claude_trigger.store")

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    """Thin wrapper around sqlite3.Connection with a single shared lock.

    Telethon dispatches updates on its own asyncio task and the FastMCP
    HTTP server calls tools on uvicorn worker threads, so the connection
    can be touched from multiple threads. We serialize access with a
    threading.Lock; the connection itself is opened with
    check_same_thread=False so it is safely shareable.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def _ensure_migrations_table(db: Database) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            sha256     TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )
    db.commit()


def _applied_versions(db: Database) -> dict[str, str]:
    rows = db.fetchall("SELECT version, sha256 FROM schema_migrations")
    return {v: h for v, h in rows}


def _run_migrations(db: Database) -> None:
    _ensure_migrations_table(db)
    applied = _applied_versions(db)

    if not _MIGRATIONS_DIR.is_dir():
        logger.warning("Migrations dir missing: %s", _MIGRATIONS_DIR)
        return

    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    now = int(time.time())
    for path in files:
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        prev = applied.get(version)
        if prev is not None:
            if prev != sha:
                # Migration file mutated after being applied — surface
                # loudly. Editing committed migrations is a footgun; the
                # right move is a new migration that supersedes.
                raise RuntimeError(
                    f"Migration {version} sha256 mismatch "
                    f"(applied={prev[:12]}..., current={sha[:12]}...). "
                    f"Edit a new migration file instead of mutating an applied one."
                )
            continue
        logger.info("Applying migration %s", version)
        with db._lock:
            db.conn.executescript(sql)
            db.conn.execute(
                "INSERT INTO schema_migrations(version, sha256, applied_at) "
                "VALUES (?, ?, ?)",
                (version, sha, now),
            )
            db.conn.commit()
        print(f"[telegram-mcp] applied migration {version}", file=sys.stderr)


def open_database(path: str) -> Database:
    """Open the SQLite database at `path`, run pending migrations, return wrapper."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    db = Database(conn)
    _run_migrations(db)
    return db
