import time
from dataclasses import dataclass

from .db import Database


@dataclass
class TrustedUser:
    user_id: int
    added_at: int
    note: str | None


class TrustedUserStore:
    """Allowlist of Telegram user_ids permitted to invoke @claude.

    The owner (TriggerConfig.owner_user_id) is implicitly trusted and
    not stored in this table — owner-bypass is enforced at the handler
    layer.
    """

    def __init__(self, db: Database):
        self._db = db

    def is_trusted(self, user_id: int) -> bool:
        if user_id <= 0:
            return False
        row = self._db.fetchone(
            "SELECT 1 FROM trusted_users WHERE user_id = ?", (user_id,)
        )
        return row is not None

    def add(self, user_id: int, note: str | None = None) -> bool:
        """Returns True if a row was inserted, False if the user_id was already trusted."""
        if user_id <= 0:
            raise ValueError("user_id must be a positive Telegram user_id")
        now = int(time.time())
        cur = self._db.execute(
            "INSERT OR IGNORE INTO trusted_users(user_id, added_at, note) "
            "VALUES (?, ?, ?)",
            (user_id, now, note),
        )
        self._db.commit()
        return cur.rowcount > 0

    def remove(self, user_id: int) -> bool:
        """Returns True if a row was deleted, False if the user_id wasn't trusted."""
        cur = self._db.execute(
            "DELETE FROM trusted_users WHERE user_id = ?", (user_id,)
        )
        self._db.commit()
        return cur.rowcount > 0

    def list(self) -> list[TrustedUser]:
        rows = self._db.fetchall(
            "SELECT user_id, added_at, note FROM trusted_users ORDER BY added_at"
        )
        return [TrustedUser(user_id=r[0], added_at=r[1], note=r[2]) for r in rows]
