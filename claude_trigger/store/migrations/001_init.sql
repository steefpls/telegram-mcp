-- 001_init.sql — initial schema for the @claude trigger pipeline.
-- Tables here cover Phase 2 MVP: trusted-user allowlist + per-chat
-- session tracking for `claude --resume`. Message/reaction/edit-history
-- tables are intentionally deferred to a later migration once the
-- features that need them (compaction, media inlining, reaction
-- formatting) are scoped.

CREATE TABLE IF NOT EXISTS trusted_users (
    user_id     INTEGER PRIMARY KEY,
    added_at    INTEGER NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id            INTEGER PRIMARY KEY,
    session_id         TEXT NOT NULL,
    newest_msg_id      INTEGER NOT NULL DEFAULT 0,
    last_input_tokens  INTEGER NOT NULL DEFAULT 0,
    just_compacted     INTEGER NOT NULL DEFAULT 0,
    last_used          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_last_used
    ON chat_sessions(last_used);
