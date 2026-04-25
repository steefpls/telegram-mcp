"""Background media auto-download for the @claude trigger.

The daemon's NewMessage handler hands every message with attached media to
`schedule_download` BEFORE running the @claude dispatch check. That keeps the
download running in parallel with whatever the trigger pipeline is doing, so
by the time the pipeline calls `MediaStore.wait_for_pending` the file is
usually already on disk and a `tg://media/{chat_id}/{message_id}` resource
read returns immediately.

Filters: kind (photo/voice/document/...) and size (MEDIA_AUTO_DOWNLOAD_MAX_SIZE_MB).
Anything filtered out lands in the DB as `skipped` so the prompt formatter can
still render an "attachment present but not downloaded" line, and the resource
template returns a small text stub if Claude tries to read it anyway.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telethon.tl.custom import Message
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
)

from .config import TriggerConfig
from .store.media import MediaStore

logger = logging.getLogger("telegram_mcp.claude_trigger.media")


# Background tasks per (chat_id, message_id) so we don't spawn duplicates if a
# MessageEdited event fires for a message whose download is still in-flight.
# Keyed weakly via a plain dict — entries cleaned up by the task itself on
# completion.
_inflight: dict[tuple[int, int], asyncio.Task] = {}


def _detect_kind_and_meta(
    msg: Message,
) -> tuple[str, Optional[str], Optional[str], Optional[int]] | None:
    """Inspect msg.media and return (kind, mime_type, file_name, file_size).

    Returns None if the message has no media we care about (text-only,
    web-page preview, contact card, etc.).
    """
    media = getattr(msg, "media", None)
    if media is None:
        return None

    # MessageMediaPhoto — photos have no filename/mime in the API; we'll use
    # generic .jpg downstream. file_size unknown until download completes.
    if getattr(msg, "photo", None) is not None:
        return ("photo", "image/jpeg", None, None)

    # Everything else flows through .document. Telethon's Message exposes
    # convenience properties (m.video, m.voice, m.audio, m.gif, m.sticker,
    # m.video_note) that are truthy when the document carries the matching
    # attribute. Check the most specific kinds first.
    doc = getattr(msg, "document", None)
    if doc is None:
        return None

    mime = getattr(doc, "mime_type", None)
    size = getattr(doc, "size", None)

    file_name = None
    is_voice = False
    is_video = False
    is_video_note = False
    is_audio = False
    is_animated = False
    is_sticker = False
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename):
            file_name = attr.file_name
        elif isinstance(attr, DocumentAttributeAudio):
            if getattr(attr, "voice", False):
                is_voice = True
            else:
                is_audio = True
        elif isinstance(attr, DocumentAttributeVideo):
            if getattr(attr, "round_message", False):
                is_video_note = True
            else:
                is_video = True
        elif isinstance(attr, DocumentAttributeAnimated):
            is_animated = True
        elif isinstance(attr, DocumentAttributeSticker):
            is_sticker = True

    if is_voice:
        return ("voice", mime or "audio/ogg", file_name, size)
    if is_video_note:
        return ("video_note", mime or "video/mp4", file_name, size)
    if is_animated:
        return ("gif", mime or "video/mp4", file_name, size)
    if is_sticker:
        return ("sticker", mime or "image/webp", file_name, size)
    if is_video:
        return ("video", mime or "video/mp4", file_name, size)
    if is_audio:
        return ("audio", mime or "audio/mpeg", file_name, size)

    # Generic document — could be PDF, text, archive, etc.
    return ("document", mime, file_name, size)


def _safe_filename(raw: Optional[str], message_id: int, mime: Optional[str]) -> str:
    """Sanitize a filename so it's safe to write to disk on Windows + POSIX.

    Telegram filenames can contain path separators, NUL bytes, leading dots,
    or be empty. We strip all of that and fall back to `<message_id>.<ext>`
    derived from the mime type.
    """
    base = (raw or "").strip().replace("\x00", "")
    # Strip directory components — never trust the sender.
    base = os.path.basename(base.replace("\\", "/"))
    # Replace remaining unsafe chars (Windows-reserved + control chars).
    safe = []
    for ch in base:
        if ch.isalnum() or ch in "._-+":
            safe.append(ch)
        else:
            safe.append("_")
    base = "".join(safe).strip("._")
    if not base:
        base = f"{message_id}{_ext_for_mime(mime)}"
    # Always prefix with the message_id so two attachments in the same chat
    # with the same suggested filename don't collide.
    if not base.startswith(f"{message_id}_"):
        base = f"{message_id}_{base}"
    return base[:200]  # Windows MAX_PATH guard


def _ext_for_mime(mime: Optional[str]) -> str:
    if not mime:
        return ".bin"
    table = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }
    return table.get(mime, ".bin")


def _build_target_path(media_dir: str, message_id: int, file_name: str) -> Path:
    now = datetime.now(timezone.utc)
    sub = Path(media_dir) / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / file_name


async def _download_one(
    client,
    msg: Message,
    *,
    chat_id: int,
    message_id: int,
    target_path: Path,
    store: MediaStore,
) -> None:
    try:
        store.mark_downloading(chat_id, message_id)
        # Strip extension so Telethon auto-detects from content (matches the
        # existing download_media tool's behavior — see main.py:2159 comment).
        out_for_dl = target_path.with_suffix("")
        downloaded = await client.download_media(msg, file=str(out_for_dl))
        if not downloaded:
            store.mark_failed(chat_id, message_id, error="Telethon returned None")
            print(
                f"[CLAUDE] Media download returned None for chat={chat_id} "
                f"msg={message_id}",
                file=sys.stderr,
            )
            return
        final = Path(downloaded).resolve()
        size = final.stat().st_size if final.exists() else None
        store.mark_downloaded(
            chat_id, message_id, file_path=str(final), file_size=size
        )
        print(
            f"[CLAUDE] Media downloaded chat={chat_id} msg={message_id} "
            f"path={final.name} size={size}",
            file=sys.stderr,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        store.mark_failed(chat_id, message_id, error=repr(e))
        print(
            f"[CLAUDE] Media download failed chat={chat_id} msg={message_id}: {e!r}",
            file=sys.stderr,
        )
        logger.exception("Media download failed")
    finally:
        _inflight.pop((chat_id, message_id), None)


def schedule_download(
    client,
    msg: Message,
    *,
    cfg: TriggerConfig,
    store: MediaStore,
) -> None:
    """Inspect `msg`, register a media row, and (if filters pass) kick off a
    background download task. Safe to call on every NewMessage — text-only
    messages and already-scheduled downloads short-circuit cheaply.
    """
    if not cfg.media_auto_download_enabled:
        return
    chat_id = msg.chat_id
    message_id = msg.id
    if chat_id is None or message_id is None:
        return

    detected = _detect_kind_and_meta(msg)
    if detected is None:
        return
    kind, mime, file_name, file_size = detected

    key = (chat_id, message_id)
    if key in _inflight:
        return  # already scheduled in this process

    # Filter: kind not in allowed set.
    if kind not in cfg.media_types:
        store.insert_skipped(
            chat_id=chat_id,
            message_id=message_id,
            kind=kind,
            mime_type=mime,
            file_name=file_name,
            file_size=file_size,
            reason=f"kind '{kind}' not in MEDIA_AUTO_DOWNLOAD_TYPES",
        )
        return

    # Filter: oversized. Photos report no size up front, so we can't filter
    # them here — the download itself will resolve. For documents/videos
    # the size is set on the Document and we can short-circuit.
    if file_size is not None and cfg.media_max_size_mb > 0:
        max_bytes = cfg.media_max_size_mb * 1024 * 1024
        if file_size > max_bytes:
            store.insert_skipped(
                chat_id=chat_id,
                message_id=message_id,
                kind=kind,
                mime_type=mime,
                file_name=file_name,
                file_size=file_size,
                reason=f"size {file_size}B exceeds MEDIA_AUTO_DOWNLOAD_MAX_SIZE_MB={cfg.media_max_size_mb}",
            )
            return

    inserted = store.insert_pending(
        chat_id=chat_id,
        message_id=message_id,
        kind=kind,
        mime_type=mime,
        file_name=file_name,
        file_size=file_size,
    )
    if not inserted:
        # Row already existed (downloaded on a prior daemon run, or another
        # in-flight task we don't know about). Don't re-queue.
        return

    safe_name = _safe_filename(file_name, message_id, mime)
    target = _build_target_path(cfg.media_dir, message_id, safe_name)

    task = asyncio.create_task(
        _download_one(
            client,
            msg,
            chat_id=chat_id,
            message_id=message_id,
            target_path=target,
            store=store,
        )
    )
    _inflight[key] = task
