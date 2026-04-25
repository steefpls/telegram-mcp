-- 002_media.sql — media auto-download tracking for the @claude trigger.
-- One row per (chat_id, message_id) holding download status + file path + mime
-- so the trigger pipeline can wait briefly for in-flight downloads, render
-- inline `tg://media/{chat_id}/{message_id}` resource pointers in prompts, and
-- the MCP resource template can resolve a URI back to a file on disk.
--
-- status values: pending, downloading, downloaded, failed, skipped, expired.

CREATE TABLE IF NOT EXISTS media_metadata (
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    kind         TEXT    NOT NULL,           -- photo|voice|audio|video|video_note|gif|sticker|document
    mime_type    TEXT,
    file_name    TEXT,
    file_size    INTEGER,
    file_path    TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_media_metadata_status
    ON media_metadata(status);
