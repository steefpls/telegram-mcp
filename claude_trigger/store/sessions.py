import time
import uuid
from dataclasses import dataclass

from .db import Database


@dataclass
class ChatSession:
    chat_id: int
    session_id: str
    newest_msg_id: int
    last_input_tokens: int
    just_compacted: bool
    last_used: int  # unix seconds


class ChatSessionStore:
    """Per-chat Claude session tracking with TTL expiry.

    A session is keyed by chat_id and holds the UUID we hand to
    `claude --resume <uuid>` plus the newest message id we've already
    forwarded into that session, so the resume prompt can include only
    new traffic since the last turn.
    """

    def __init__(self, db: Database, ttl_hours: int = 24):
        self._db = db
        self._ttl_seconds = ttl_hours * 3600

    def get(self, chat_id: int) -> ChatSession | None:
        row = self._db.fetchone(
            "SELECT chat_id, session_id, newest_msg_id, last_input_tokens, "
            "just_compacted, last_used FROM chat_sessions WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        s = ChatSession(
            chat_id=row[0],
            session_id=row[1],
            newest_msg_id=row[2],
            last_input_tokens=row[3],
            just_compacted=bool(row[4]),
            last_used=row[5],
        )
        if int(time.time()) - s.last_used > self._ttl_seconds:
            # Lazily evict expired rows on access. Cheap, no background sweep.
            self._db.execute("DELETE FROM chat_sessions WHERE chat_id = ?", (chat_id,))
            self._db.commit()
            return None
        return s

    def save(
        self,
        chat_id: int,
        session_id: str,
        newest_msg_id: int,
        last_input_tokens: int,
        just_compacted: bool = False,
    ) -> None:
        now = int(time.time())
        self._db.execute(
            """
            INSERT INTO chat_sessions
                (chat_id, session_id, newest_msg_id, last_input_tokens,
                 just_compacted, last_used)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                session_id = excluded.session_id,
                newest_msg_id = excluded.newest_msg_id,
                last_input_tokens = excluded.last_input_tokens,
                just_compacted = excluded.just_compacted,
                last_used = excluded.last_used
            """,
            (
                chat_id,
                session_id,
                newest_msg_id,
                last_input_tokens,
                1 if just_compacted else 0,
                now,
            ),
        )
        self._db.commit()

    def new_session_id(self) -> str:
        return str(uuid.uuid4())
