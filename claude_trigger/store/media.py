import asyncio
import time
from dataclasses import dataclass

from .db import Database

# Status values stored on media_metadata.status. Kept as module constants
# (not an Enum) because they round-trip to TEXT in SQLite and we want easy
# comparison from query results.
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"


@dataclass
class MediaRecord:
    chat_id: int
    message_id: int
    kind: str
    mime_type: str | None
    file_name: str | None
    file_size: int | None
    file_path: str | None
    status: str
    error: str | None
    created_at: int
    updated_at: int

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            STATUS_DOWNLOADED,
            STATUS_FAILED,
            STATUS_SKIPPED,
            STATUS_EXPIRED,
        )


_COLUMNS = (
    "chat_id, message_id, kind, mime_type, file_name, file_size, "
    "file_path, status, error, created_at, updated_at"
)


def _row_to_record(row: tuple) -> MediaRecord:
    return MediaRecord(
        chat_id=row[0],
        message_id=row[1],
        kind=row[2],
        mime_type=row[3],
        file_name=row[4],
        file_size=row[5],
        file_path=row[6],
        status=row[7],
        error=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


class MediaStore:
    """Per-(chat, message) media download tracking.

    Status transitions: pending -> downloading -> (downloaded | failed).
    The `skipped` state is set up-front when filters reject the media (wrong
    type or oversized), so the trigger pipeline knows not to wait. `expired`
    is reserved for future cleanup of files Telegram no longer serves.
    """

    def __init__(self, db: Database):
        self._db = db

    def insert_pending(
        self,
        *,
        chat_id: int,
        message_id: int,
        kind: str,
        mime_type: str | None,
        file_name: str | None,
        file_size: int | None,
    ) -> bool:
        """Insert a fresh pending row. Returns False if a row already exists
        for this (chat, message) — caller should treat that as 'already
        scheduled or done', not as an error."""
        now = int(time.time())
        cur = self._db.execute(
            f"INSERT OR IGNORE INTO media_metadata ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (
                chat_id,
                message_id,
                kind,
                mime_type,
                file_name,
                file_size,
                STATUS_PENDING,
                now,
                now,
            ),
        )
        self._db.commit()
        return cur.rowcount > 0

    def insert_skipped(
        self,
        *,
        chat_id: int,
        message_id: int,
        kind: str,
        mime_type: str | None,
        file_name: str | None,
        file_size: int | None,
        reason: str,
    ) -> bool:
        now = int(time.time())
        cur = self._db.execute(
            f"INSERT OR IGNORE INTO media_metadata ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                chat_id,
                message_id,
                kind,
                mime_type,
                file_name,
                file_size,
                STATUS_SKIPPED,
                reason,
                now,
                now,
            ),
        )
        self._db.commit()
        return cur.rowcount > 0

    def mark_downloading(self, chat_id: int, message_id: int) -> None:
        now = int(time.time())
        self._db.execute(
            "UPDATE media_metadata SET status = ?, updated_at = ? "
            "WHERE chat_id = ? AND message_id = ?",
            (STATUS_DOWNLOADING, now, chat_id, message_id),
        )
        self._db.commit()

    def mark_downloaded(
        self, chat_id: int, message_id: int, *, file_path: str, file_size: int | None
    ) -> None:
        now = int(time.time())
        self._db.execute(
            "UPDATE media_metadata SET status = ?, file_path = ?, file_size = "
            "COALESCE(?, file_size), error = NULL, updated_at = ? "
            "WHERE chat_id = ? AND message_id = ?",
            (STATUS_DOWNLOADED, file_path, file_size, now, chat_id, message_id),
        )
        self._db.commit()

    def mark_failed(self, chat_id: int, message_id: int, *, error: str) -> None:
        now = int(time.time())
        self._db.execute(
            "UPDATE media_metadata SET status = ?, error = ?, updated_at = ? "
            "WHERE chat_id = ? AND message_id = ?",
            (STATUS_FAILED, error[:500], now, chat_id, message_id),
        )
        self._db.commit()

    def get(self, chat_id: int, message_id: int) -> MediaRecord | None:
        row = self._db.fetchone(
            f"SELECT {_COLUMNS} FROM media_metadata "
            "WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return _row_to_record(row) if row else None

    def get_many(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, MediaRecord]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" * len(message_ids))
        rows = self._db.fetchall(
            f"SELECT {_COLUMNS} FROM media_metadata "
            f"WHERE chat_id = ? AND message_id IN ({placeholders})",
            (chat_id, *message_ids),
        )
        return {row[1]: _row_to_record(row) for row in rows}

    async def wait_for_pending(
        self,
        chat_id: int,
        message_ids: list[int],
        *,
        timeout_seconds: float,
        poll_interval: float = 0.25,
    ) -> dict[int, MediaRecord]:
        """Poll until every media row in the set reaches a terminal status,
        or `timeout_seconds` elapses. Returns the latest snapshot of records
        (which may still include pending entries on timeout — caller decides
        how to render those)."""
        if not message_ids or timeout_seconds <= 0:
            return self.get_many(chat_id, message_ids)
        deadline = time.monotonic() + timeout_seconds
        while True:
            snapshot = self.get_many(chat_id, message_ids)
            unsettled = [
                mid for mid in message_ids
                if mid in snapshot and not snapshot[mid].is_terminal
            ]
            if not unsettled:
                return snapshot
            if time.monotonic() >= deadline:
                return snapshot
            await asyncio.sleep(poll_interval)
