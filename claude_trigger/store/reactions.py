"""Reaction cache for the @claude trigger.

`_gather_history` originally fired one `GetMessageReactionsListRequest` per
message in the slice (~40 API calls per trigger). In active group chats
that's a real rate-limit risk and adds latency. This store caches the
reactor list keyed by (chat_id, message_id, emoji, user_id) and stamps
each fetch in `reaction_fetches`. The handler reads cache-first and only
falls back to the live API when `last_fetched_at` is older than
`CLAUDE_REACTION_CACHE_TTL_SECONDS` (default 300s).

Stale-read tradeoff: if a reaction is added during the TTL window, the
prompt won't see it until the next fetch refresh. For most chats that's
fine — reactions usually arrive in a burst after a message and stabilize.
"""
from __future__ import annotations

import time

from .db import Database


class ReactionCacheStore:
    def __init__(self, db: Database):
        self._db = db

    def is_fresh(
        self, chat_id: int, message_id: int, ttl_seconds: int
    ) -> bool:
        if ttl_seconds <= 0:
            return False
        row = self._db.fetchone(
            "SELECT last_fetched_at FROM reaction_fetches "
            "WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        if row is None:
            return False
        return (int(time.time()) - row[0]) < ttl_seconds

    def get_cached_pairs(
        self, chat_id: int, message_id: int
    ) -> list[tuple[str, int]]:
        """Return [(emoji, user_id), ...] from cache. Empty list if nothing
        cached (caller distinguishes 'no reactions' from 'never fetched'
        via is_fresh)."""
        rows = self._db.fetchall(
            "SELECT emoji, user_id FROM reactions_cache "
            "WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return [(r[0], r[1]) for r in rows]

    def replace(
        self,
        *,
        chat_id: int,
        message_id: int,
        pairs: list[tuple[str, int]],
    ) -> None:
        """Atomically replace the cached reactor set for one message and
        bump the fetch timestamp. Empty `pairs` is a valid state — means
        the message has no reactions, and we now know that authoritatively."""
        now = int(time.time())
        # Drop existing rows then re-insert. Cheaper than diffing for the
        # tiny row counts involved (typically <20 reactors per message).
        self._db.execute(
            "DELETE FROM reactions_cache WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        for emoji, user_id in pairs:
            self._db.execute(
                "INSERT OR IGNORE INTO reactions_cache "
                "(chat_id, message_id, emoji, user_id) VALUES (?, ?, ?, ?)",
                (chat_id, message_id, emoji, user_id),
            )
        self._db.execute(
            """
            INSERT INTO reaction_fetches (chat_id, message_id, last_fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                last_fetched_at = excluded.last_fetched_at
            """,
            (chat_id, message_id, now),
        )
        self._db.commit()

    def invalidate(self, chat_id: int, message_id: int) -> None:
        """Force the next read to refetch live. Called from the MessageEdited
        handler — Telegram delivers reaction adds/removes through the same
        update channel, so an edit event is a strong signal something changed."""
        self._db.execute(
            "DELETE FROM reaction_fetches WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        self._db.commit()
