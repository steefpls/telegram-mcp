"""Message persistence + edit history.

Two responsibilities, one store: keep the latest known text for every
(chat_id, message_id) we observe, and append a row to `message_edits`
each time a NewMessage's text actually changes via MessageEdited.

The store is intentionally non-authoritative — Telegram is the source of
truth for the live message text. We use it solely to surface edit history
in the @claude prompt ("originally said X, edited to Y"), which Telethon's
live `iter_messages` can't reconstruct on its own (it only ever returns
current text).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database


@dataclass
class MessageEdit:
    chat_id: int
    message_id: int
    edit_seq: int
    prior_text: str
    edited_at: int  # unix seconds


class MessageStore:
    def __init__(self, db: Database):
        self._db = db

    def record_initial(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int,
        text: str,
    ) -> None:
        """Insert a row at first observation. No-op if a row already exists
        (idempotent — daemon restarts replay events for messages we've
        already seen, and a NewMessage we somehow get twice shouldn't
        clobber edit history)."""
        if not text:
            return
        now = int(time.time())
        self._db.execute(
            """
            INSERT OR IGNORE INTO messages
                (chat_id, message_id, sender_id, original_text, current_text,
                 revision_count, seen_at, last_revised_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, NULL)
            """,
            (chat_id, message_id, sender_id, text, text, now),
        )
        self._db.commit()

    def record_edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int,
        new_text: str,
    ) -> bool:
        """Compare new_text against the stored current_text. If they differ
        (real text edit, not a reaction-only MessageEdited event), append a
        row to message_edits with the OLD text and update messages.

        Returns True if a revision was logged, False if the text was
        unchanged (reaction-only edit, attachment re-render, etc.) or the
        message wasn't in our store yet (logged as initial below).
        """
        if not new_text:
            return False
        row = self._db.fetchone(
            "SELECT current_text, revision_count FROM messages "
            "WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        now = int(time.time())
        if row is None:
            # First time we see this message is via an edit (daemon was down
            # when it was sent). Capture current_text as both original and
            # current — we missed the actual original.
            self._db.execute(
                """
                INSERT INTO messages
                    (chat_id, message_id, sender_id, original_text, current_text,
                     revision_count, seen_at, last_revised_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, NULL)
                """,
                (chat_id, message_id, sender_id, new_text, new_text, now),
            )
            self._db.commit()
            return False

        prior_text, revision_count = row
        if prior_text == new_text:
            return False  # MessageEdited fired but text unchanged (likely a reaction update)

        next_seq = (revision_count or 0) + 1
        self._db.execute(
            """
            INSERT INTO message_edits
                (chat_id, message_id, edit_seq, prior_text, edited_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, next_seq, prior_text, now),
        )
        self._db.execute(
            """
            UPDATE messages SET current_text = ?, revision_count = ?, last_revised_at = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            (new_text, next_seq, now, chat_id, message_id),
        )
        self._db.commit()
        return True

    def get_edits_for_slice(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, list[MessageEdit]]:
        """Bulk fetch every edit for messages in the slice, ordered oldest-first
        per message. Empty dict if no edits exist anywhere in the slice."""
        if not message_ids:
            return {}
        placeholders = ",".join("?" * len(message_ids))
        rows = self._db.fetchall(
            f"SELECT chat_id, message_id, edit_seq, prior_text, edited_at "
            f"FROM message_edits "
            f"WHERE chat_id = ? AND message_id IN ({placeholders}) "
            f"ORDER BY message_id, edit_seq",
            (chat_id, *message_ids),
        )
        out: dict[int, list[MessageEdit]] = {}
        for r in rows:
            edit = MessageEdit(
                chat_id=r[0],
                message_id=r[1],
                edit_seq=r[2],
                prior_text=r[3],
                edited_at=r[4],
            )
            out.setdefault(edit.message_id, []).append(edit)
        return out
