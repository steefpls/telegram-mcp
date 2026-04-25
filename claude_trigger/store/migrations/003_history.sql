-- 003_history.sql — message persistence + edit log + reactions cache.
--
-- Two features land on this migration:
--
-- 1. Edit history — `messages` holds the latest text per (chat, message);
--    `message_edits` is an append-only log of every PRIOR text version
--    (one row per edit, capturing what the message looked like BEFORE the
--    edit). Gives the @claude prompt a "originally said X, edited to Y"
--    line so the model has the full conversational signal that Telegram's
--    "(edited)" marker only hints at.
--
-- 2. Reaction cache — every fetched reactor goes into `reactions_cache`
--    with the fetch timestamp tracked in `reaction_fetches`. Drops the
--    per-trigger API hit from ~40 GetMessageReactionsListRequest calls to
--    ~0 in steady-state (only stale or never-fetched messages re-hit live).

CREATE TABLE IF NOT EXISTS messages (
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    sender_id        INTEGER NOT NULL DEFAULT 0,
    original_text    TEXT    NOT NULL,
    current_text     TEXT    NOT NULL,
    revision_count   INTEGER NOT NULL DEFAULT 0,   -- 0 == never edited
    seen_at          INTEGER NOT NULL,             -- unix seconds; first observation
    last_revised_at  INTEGER,                      -- unix seconds; nullable
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS message_edits (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    edit_seq     INTEGER NOT NULL,                 -- 1-based per (chat, message)
    prior_text   TEXT    NOT NULL,                 -- text BEFORE this edit
    edited_at    INTEGER NOT NULL,                 -- unix seconds
    PRIMARY KEY (chat_id, message_id, edit_seq)
);

CREATE INDEX IF NOT EXISTS idx_message_edits_msg
    ON message_edits(chat_id, message_id);

CREATE TABLE IF NOT EXISTS reactions_cache (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    emoji        TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,                 -- 0 == anonymous / channel reaction
    PRIMARY KEY (chat_id, message_id, emoji, user_id)
);

CREATE INDEX IF NOT EXISTS idx_reactions_cache_msg
    ON reactions_cache(chat_id, message_id);

CREATE TABLE IF NOT EXISTS reaction_fetches (
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    last_fetched_at  INTEGER NOT NULL,             -- unix seconds
    PRIMARY KEY (chat_id, message_id)
);
