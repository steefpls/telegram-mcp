import asyncio
import logging
import sys
from datetime import datetime, timezone

from telethon import events
from telethon.tl.custom import Message
from telethon.tl.functions.messages import GetMessageReactionsListRequest
from telethon.tl.types import MessageReplyHeader

from .config import TriggerConfig
from .locks import ChatLockManager
from .media import schedule_download
from .messages import (
    is_claude_response,
    is_thinking_message,
    random_signature,
    random_thinking,
)
from .prompt import (
    HistMsg,
    MediaInfo,
    build_compaction_summary_prompt,
    build_post_compact_prompt,
    build_prompt,
    build_resume_prompt,
)
from .runner import ClaudeResult, run_claude
from .store import (
    ChatSessionStore,
    MediaStore,
    MessageStore,
    ReactionCacheStore,
    TrustedUserStore,
)

# Fallback retry pause between attempt 2 (fresh after resume failure) and
# attempt 3 (sleep+retry fresh). Short enough that the user doesn't notice;
# long enough to ride out a transient stdio/anyio hiccup or rate-limit nudge.
_FALLBACK_RETRY_DELAY_SECONDS = 2.0

logger = logging.getLogger("telegram_mcp.claude_trigger")


async def _resolve_sender_name(client, sender_id: int) -> str:
    if not sender_id:
        return "Unknown"
    try:
        ent = await client.get_entity(sender_id)
    except Exception:
        return f"User-{sender_id}"
    first = (getattr(ent, "first_name", None) or "").strip()
    last = (getattr(ent, "last_name", None) or "").strip()
    name = (first + " " + last).strip()
    if name:
        return name
    username = (getattr(ent, "username", None) or "").strip()
    return username or f"User-{sender_id}"


async def _resolve_chat_name(client, chat_id: int) -> str:
    try:
        ent = await client.get_entity(chat_id)
    except Exception:
        return f"Chat-{chat_id}"
    title = getattr(ent, "title", None)
    if title:
        return title
    first = (getattr(ent, "first_name", None) or "").strip()
    last = (getattr(ent, "last_name", None) or "").strip()
    full = (first + " " + last).strip()
    return full or f"Chat-{chat_id}"


async def _gather_history(
    client,
    chat_id: int,
    limit: int,
    owner_id: int,
    media_store: MediaStore | None = None,
    message_store: MessageStore | None = None,
    reaction_cache: ReactionCacheStore | None = None,
    reaction_cache_ttl: int = 0,
    show_edit_history: bool = True,
) -> list[HistMsg]:
    raw: list[Message] = []
    async for m in client.iter_messages(chat_id, limit=limit):
        text = m.text or ""
        if is_thinking_message(text):
            continue
        # Keep messages with attached media even when text is empty — the
        # prompt formatter will render the media line, and dropping these
        # would leave Claude blind to photo/voice replies that came in
        # without a caption.
        if not text and not getattr(m, "media", None):
            continue
        raw.append(m)

    # Per-call name cache so we resolve each user at most once across
    # message senders, reactors, and quote-reply parents.
    name_cache: dict[int, str] = {0: "Unknown"}
    if owner_id:
        name_cache[owner_id] = "Steve"

    async def name_for(uid: int) -> str:
        if uid in name_cache:
            return name_cache[uid]
        n = await _resolve_sender_name(client, uid)
        name_cache[uid] = n
        return n

    # --- Bulk fetch parent messages for any quote-replies in the slice ---
    reply_ids: list[int] = []
    for m in raw:
        rt = getattr(m, "reply_to", None)
        if not isinstance(rt, MessageReplyHeader):
            continue
        # Cross-chat replies (reply_to_peer_id set) point at a different
        # peer; skip — fetching from the wrong chat would error and the
        # parent isn't part of *this* conversation anyway.
        if getattr(rt, "reply_to_peer_id", None):
            continue
        pid = getattr(rt, "reply_to_msg_id", None)
        if pid:
            reply_ids.append(pid)
    parents: dict[int, Message] = {}
    if reply_ids:
        try:
            fetched = await client.get_messages(chat_id, ids=reply_ids)
            for p in fetched:
                if p is not None:
                    parents[p.id] = p
        except Exception as e:
            print(
                f"[CLAUDE] Quote-parent fetch failed for chat {chat_id}: {e!r}",
                file=sys.stderr,
            )

    # --- Per-message reactor lookups (cache-first) ---
    # Cache hit path: read (emoji, user_id) pairs from reactions_cache, no
    # API call. Cache miss / stale: fire GetMessageReactionsListRequest,
    # store the result. Drops typical per-trigger reaction-list API call
    # count from len(history-with-reactions) to ~0 in steady state.
    async def fetch_reactions_for(m: Message) -> list[tuple[str, str]]:
        if not getattr(m, "reactions", None):
            return []

        cache_hit = (
            reaction_cache is not None
            and reaction_cache.is_fresh(chat_id, m.id, reaction_cache_ttl)
        )
        if cache_hit:
            pairs = reaction_cache.get_cached_pairs(chat_id, m.id)
            grouped: dict[str, list[int]] = {}
            for emoji, uid in pairs:
                grouped.setdefault(emoji, []).append(uid)
            unique_uids = {u for uids in grouped.values() for u in uids}
            for uid in unique_uids:
                await name_for(uid)
            return [
                (emoji, ", ".join(name_cache[uid] for uid in uids))
                for emoji, uids in grouped.items()
            ]

        # Cache miss / stale — fall back to live API.
        try:
            res = await client(
                GetMessageReactionsListRequest(peer=chat_id, id=m.id, limit=20)
            )
        except Exception:
            res = None
        if res is not None and getattr(res, "reactions", None):
            grouped = {}
            for pr in res.reactions:
                emoji = getattr(pr.reaction, "emoticon", None) or "?"
                uid = getattr(pr.peer_id, "user_id", None) or 0
                grouped.setdefault(emoji, []).append(uid)
            # Persist into cache so the next trigger inside the TTL skips the API.
            if reaction_cache is not None:
                pairs = [
                    (emoji, uid)
                    for emoji, uids in grouped.items()
                    for uid in uids
                ]
                try:
                    reaction_cache.replace(
                        chat_id=chat_id, message_id=m.id, pairs=pairs
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist reaction cache for msg=%s", m.id
                    )
            unique_uids = {u for uids in grouped.values() for u in uids}
            for uid in unique_uids:
                await name_for(uid)
            return [
                (emoji, ", ".join(name_cache[uid] for uid in uids))
                for emoji, uids in grouped.items()
            ]
        # Fallback: emoji + count, no names. Some chat types refuse the
        # reactions-list request (private DMs without seen-by, channels
        # without reaction visibility) but still expose .reactions counts.
        return [
            (getattr(r.reaction, "emoticon", None) or "?", f"×{r.count}")
            for r in m.reactions.results
        ]

    react_lists = await asyncio.gather(
        *(fetch_reactions_for(m) for m in raw),
        return_exceptions=True,
    )

    # Bulk-fetch media records for the slice in one query.
    media_by_msg_id: dict[int, "MediaInfo"] = {}
    if media_store is not None:
        msg_ids_with_media = [m.id for m in raw if getattr(m, "media", None)]
        records = media_store.get_many(chat_id, msg_ids_with_media) if msg_ids_with_media else {}
        for mid, rec in records.items():
            media_by_msg_id[mid] = MediaInfo(
                chat_id=rec.chat_id,
                message_id=rec.message_id,
                kind=rec.kind,
                mime_type=rec.mime_type,
                file_name=rec.file_name,
                file_size=rec.file_size,
                status=rec.status,
            )

    # Bulk-fetch edit history for the slice in one query.
    edits_by_msg_id: dict[int, list[str]] = {}
    if message_store is not None and show_edit_history:
        slice_ids = [m.id for m in raw]
        edits_map = message_store.get_edits_for_slice(chat_id, slice_ids)
        for mid, edits in edits_map.items():
            edits_by_msg_id[mid] = [e.prior_text for e in edits]

    out: list[HistMsg] = []
    for m, reactions in zip(raw, react_lists):
        if isinstance(reactions, BaseException) or reactions is None:
            reactions = []

        sender_id = m.sender_id or 0
        is_owner = sender_id == owner_id
        sender_name = await name_for(sender_id)
        ts = m.date or datetime.now(timezone.utc)

        reply_preview: tuple[str, str] | None = None
        rt = getattr(m, "reply_to", None)
        if isinstance(rt, MessageReplyHeader):
            pid = getattr(rt, "reply_to_msg_id", None)
            parent = parents.get(pid) if pid else None
            ptext = getattr(parent, "text", None) if parent is not None else None
            if parent is not None and ptext:
                parent_sender = await name_for(parent.sender_id or 0)
                reply_preview = (parent_sender, ptext)

        out.append(
            HistMsg(
                sender_id=sender_id,
                sender_name=sender_name,
                timestamp=ts,
                text=m.text or "",
                is_owner=is_owner,
                id=m.id,
                reactions=reactions,
                reply_preview=reply_preview,
                media=media_by_msg_id.get(m.id),
                edit_history=edits_by_msg_id.get(m.id, []),
            )
        )
    out.reverse()  # oldest first
    return out


def _build_event_handler(
    client,
    cfg: TriggerConfig,
    locks: ChatLockManager,
    sessions: ChatSessionStore,
    trusted: TrustedUserStore,
    media: MediaStore | None,
    messages: MessageStore | None,
    reactions: ReactionCacheStore | None,
    mcp=None,
):
    async def handler(event):
        msg = getattr(event, "message", None)
        # Always kick off media auto-download for any message we observe,
        # regardless of @claude — so when a trigger fires later the file is
        # already on disk. schedule_download is cheap (no-op for text-only,
        # idempotent per message_id).
        if msg is not None and media is not None:
            try:
                schedule_download(client, msg, cfg=cfg, store=media, mcp=mcp)
            except Exception:
                logger.exception("Failed to schedule media download")
        # Persist message text and log edits. Telethon delivers both
        # NewMessage and MessageEdited as the same event shape; we
        # disambiguate by event class name.
        if msg is not None and messages is not None:
            try:
                # Telethon names both event classes "Event" — the discriminator
                # lives in __qualname__: "NewMessage.Event" vs "MessageEdited.Event".
                evt_qualname = type(event).__qualname__
                text = msg.text or ""
                if text and not is_thinking_message(text):
                    if "MessageEdited" in evt_qualname:
                        # On edit, log a revision (only if text actually
                        # changed — reaction-only events go through this
                        # path too) and invalidate the reaction cache so
                        # the next trigger re-fetches.
                        messages.record_edit(
                            chat_id=msg.chat_id,
                            message_id=msg.id,
                            sender_id=msg.sender_id or 0,
                            new_text=text,
                        )
                        if reactions is not None:
                            reactions.invalidate(msg.chat_id, msg.id)
                    else:
                        messages.record_initial(
                            chat_id=msg.chat_id,
                            message_id=msg.id,
                            sender_id=msg.sender_id or 0,
                            text=text,
                        )
            except Exception:
                logger.exception("Failed to persist message / edit")
        try:
            await _maybe_dispatch(
                client, cfg, locks, sessions, trusted, media, messages, reactions, event
            )
        except Exception:
            logger.exception("Unhandled error in @claude event handler")

    return handler


async def _maybe_dispatch(
    client,
    cfg: TriggerConfig,
    locks: ChatLockManager,
    sessions: ChatSessionStore,
    trusted: TrustedUserStore,
    media: MediaStore | None,
    messages: MessageStore | None,
    reactions: ReactionCacheStore | None,
    event,
) -> None:
    msg: Message = event.message
    text = msg.text or ""
    if "@claude" not in text.lower():
        return
    # Loop guard: ignore Claude's own past responses (signature suffix)
    if is_claude_response(text):
        return
    # Loop guard: thinking acks contain "@claude" sometimes? No — but defensive.
    if is_thinking_message(text):
        return

    sender_id = msg.sender_id
    is_owner = sender_id == cfg.owner_user_id
    if not is_owner and not trusted.is_trusted(sender_id or 0):
        print(
            f"[CLAUDE] Ignoring @claude from untrusted sender {sender_id} "
            f"(owner={cfg.owner_user_id})",
            file=sys.stderr,
        )
        return

    chat_id = msg.chat_id
    lock = locks.get(chat_id)
    async with lock:
        await _process_trigger(
            client, cfg, sessions, media, messages, reactions,
            msg, chat_id, sender_id,
        )


async def _run_with_fallback(
    cfg: TriggerConfig,
    *,
    chat_id: int,
    primary_prompt: str,
    primary_resume_session_id: str,
    fallback_prompt: str,
) -> ClaudeResult:
    """Three-attempt invocation chain.

    1. Primary call (resume or fresh, as decided upstream).
    2. If primary was a resume AND failed, retry once with a fresh session
       and the full `fallback_prompt` (the resumed session may itself be
       broken on Claude's end — a fresh session bypasses it).
    3. If still failing, sleep briefly and retry fresh once more to ride
       out transient issues (stdio hiccups, rate-limit nudges, anyio
       stream resets from the FastMCP HTTP transport).

    Returns the final ClaudeResult — caller handles the error message edit
    on failure exactly like before.
    """
    result = await run_claude(
        primary_prompt,
        claude_path=cfg.claude_path,
        model=cfg.claude_model,
        max_budget_usd=cfg.max_budget_usd,
        timeout_seconds=cfg.timeout_seconds,
        mcp_port=cfg.mcp_port,
        mcp_api_key=cfg.mcp_api_key,
        resume_session_id=primary_resume_session_id,
    )
    if result.success:
        return result

    if primary_resume_session_id:
        print(
            f"[CLAUDE] Fallback 2/3 (chat {chat_id}): resume "
            f"{primary_resume_session_id} failed ({result.error!r}), "
            f"retrying as fresh session",
            file=sys.stderr,
        )
        result = await run_claude(
            fallback_prompt,
            claude_path=cfg.claude_path,
            model=cfg.claude_model,
            max_budget_usd=cfg.max_budget_usd,
            timeout_seconds=cfg.timeout_seconds,
            mcp_port=cfg.mcp_port,
            mcp_api_key=cfg.mcp_api_key,
            resume_session_id="",
        )
        if result.success:
            return result

    print(
        f"[CLAUDE] Fallback 3/3 (chat {chat_id}): retrying fresh after "
        f"{_FALLBACK_RETRY_DELAY_SECONDS}s sleep ({result.error!r})",
        file=sys.stderr,
    )
    await asyncio.sleep(_FALLBACK_RETRY_DELAY_SECONDS)
    result = await run_claude(
        fallback_prompt,
        claude_path=cfg.claude_path,
        model=cfg.claude_model,
        max_budget_usd=cfg.max_budget_usd,
        timeout_seconds=cfg.timeout_seconds,
        mcp_port=cfg.mcp_port,
        mcp_api_key=cfg.mcp_api_key,
        resume_session_id="",
    )
    return result


async def _process_trigger(
    client,
    cfg: TriggerConfig,
    sessions: ChatSessionStore,
    media: MediaStore | None,
    messages: MessageStore | None,
    reactions: ReactionCacheStore | None,
    msg: Message,
    chat_id: int,
    sender_id: int,
) -> None:
    # Send thinking ack as a reply to the trigger message
    try:
        ack = await client.send_message(chat_id, random_thinking(), reply_to=msg.id)
    except Exception as e:
        print(f"[CLAUDE] Failed to send ack to chat {chat_id}: {e!r}", file=sys.stderr)
        logger.exception("Failed to send thinking ack")
        return

    try:
        gather_kwargs = dict(
            media_store=media,
            message_store=messages,
            reaction_cache=reactions,
            reaction_cache_ttl=cfg.reaction_cache_ttl_seconds,
            show_edit_history=cfg.show_edit_history,
        )
        # First pass: gather history without waiting for media (cheap), so we
        # know which message_ids in the slice carry attachments.
        history = await _gather_history(
            client, chat_id, cfg.history_messages, cfg.owner_user_id, **gather_kwargs
        )

        # Wait briefly for any in-flight downloads in the slice. This lets a
        # photo Steve sent ~2 seconds before the @claude trigger settle into
        # `downloaded` so the prompt formatter can render the resource URI
        # with confidence rather than a "still in flight" stub.
        if media is not None and cfg.media_wait_timeout_seconds > 0:
            pending_ids = [
                m.id for m in history
                if m.media is not None and m.media.status in ("pending", "downloading")
            ]
            if pending_ids:
                print(
                    f"[CLAUDE] Waiting up to {cfg.media_wait_timeout_seconds}s for "
                    f"{len(pending_ids)} pending media download(s) in chat {chat_id}",
                    file=sys.stderr,
                )
                await media.wait_for_pending(
                    chat_id,
                    pending_ids,
                    timeout_seconds=cfg.media_wait_timeout_seconds,
                )
                # Re-gather so HistMsg.media reflects the latest statuses.
                history = await _gather_history(
                    client, chat_id, cfg.history_messages, cfg.owner_user_id,
                    **gather_kwargs,
                )

        chat_name = await _resolve_chat_name(client, chat_id)
        requester_is_owner = sender_id == cfg.owner_user_id
        requester_name = (
            "Steve" if requester_is_owner else await _resolve_sender_name(client, sender_id)
        )

        # --- Resume vs new-session vs compact decision ------------------
        # Resume only when: a session exists, isn't expired, AND there is
        # no gap between the messages it has already seen and the oldest
        # message in our current history slice (otherwise the session
        # would be reasoning over a hole in the conversation).
        existing = sessions.get(chat_id)
        oldest_in_history = min((m.id for m in history), default=0)
        can_resume = (
            existing is not None
            and oldest_in_history > 0
            and oldest_in_history <= existing.newest_msg_id
        )

        # Compaction fires when the resumable session is approaching the
        # context window. We require can_resume because there is nothing to
        # compact otherwise (a fresh session has no prior context to summarize),
        # and we gate on `just_compacted` so two compactions never run
        # back-to-back (the post-compact turn itself can be heavy).
        threshold_met = (
            can_resume
            and cfg.compact_threshold_tokens > 0
            and existing is not None
            and existing.last_input_tokens >= cfg.compact_threshold_tokens
        )
        if threshold_met and existing is not None and existing.just_compacted:
            print(
                f"[CLAUDE] Session {existing.session_id} for chat {chat_id} "
                f"exceeded threshold ({existing.last_input_tokens} >= "
                f"{cfg.compact_threshold_tokens}) but was just compacted last "
                f"turn — skipping to avoid loop",
                file=sys.stderr,
            )
        should_compact = threshold_met and existing is not None and not existing.just_compacted

        # Always build the full new-session prompt so the fallback chain can
        # retry as fresh if a resume or post-compact attempt fails. Cheap —
        # build_prompt is pure string assembly over `history`.
        fresh_prompt = build_prompt(
            chat_name=chat_name,
            requester_name=requester_name,
            requester_is_owner=requester_is_owner,
            messages=history,
            latest_text=msg.text or "",
            memory_vault=cfg.memory_vault,
        )

        compacted_now = False
        if should_compact:
            assert existing is not None  # mypy/sanity — guaranteed by should_compact
            print(
                f"[CLAUDE] Session {existing.session_id} for chat {chat_id} "
                f"exceeded threshold ({existing.last_input_tokens} >= "
                f"{cfg.compact_threshold_tokens}), compacting",
                file=sys.stderr,
            )
            try:
                await client.edit_message(
                    chat_id, ack.id, "🗜️ compacting context..."
                )
            except Exception as e:
                print(
                    f"[CLAUDE] Failed to edit ack to compacting state: {e!r}",
                    file=sys.stderr,
                )

            print(
                f"[CLAUDE] Compacting session {existing.session_id} "
                f"using model={cfg.compact_model}",
                file=sys.stderr,
            )

            summary_result = await run_claude(
                build_compaction_summary_prompt(),
                claude_path=cfg.claude_path,
                model=cfg.compact_model,
                max_budget_usd=cfg.compact_max_budget_usd,
                timeout_seconds=cfg.compact_timeout_seconds,
                mcp_port=cfg.mcp_port,
                mcp_api_key=cfg.mcp_api_key,
                resume_session_id=existing.session_id,
            )

            if summary_result.success and summary_result.text.strip():
                # Build the post-compact prompt for a brand new session.
                prompt = build_post_compact_prompt(
                    chat_name=chat_name,
                    requester_name=requester_name,
                    requester_is_owner=requester_is_owner,
                    summary=summary_result.text,
                    messages=history,
                    latest_text=msg.text or "",
                    memory_vault=cfg.memory_vault,
                )
                resume_session_id = ""  # fresh session
                compacted_now = True
                print(
                    f"[CLAUDE] Compaction successful for chat {chat_id} "
                    f"(summary: {len(summary_result.text)} chars)",
                    file=sys.stderr,
                )
            else:
                # Summary failed — fall back to a normal fresh session. The
                # prior session may itself be broken; trying to resume it
                # again here would just hit the same wall.
                print(
                    f"[CLAUDE] Compaction failed for chat {chat_id}, falling "
                    f"back to fresh session without summary: {summary_result.error!r}",
                    file=sys.stderr,
                )
                prompt = fresh_prompt
                resume_session_id = ""
                # Mark as "compacted" anyway so we don't re-attempt the
                # failing summary on every subsequent turn — operator can
                # clear the flag manually or wait for the next normal turn.
                compacted_now = True
        elif can_resume:
            assert existing is not None
            # Strip messages the session already has, plus Claude's own
            # outgoing replies (the session has them in its native form;
            # re-sending the rendered text wastes tokens).
            new_messages = [
                m for m in history
                if m.id > existing.newest_msg_id
                and not (m.is_owner and is_claude_response(m.text))
            ]
            prompt = build_resume_prompt(
                requester_name=requester_name,
                requester_is_owner=requester_is_owner,
                new_messages=new_messages,
                latest_text=msg.text or "",
            )
            resume_session_id = existing.session_id
            print(
                f"[CLAUDE] Resuming session {existing.session_id} for chat "
                f"{chat_id} ({len(new_messages)} new messages, "
                f"last_tokens={existing.last_input_tokens})",
                file=sys.stderr,
            )
        else:
            prompt = fresh_prompt
            resume_session_id = ""
            print(
                f"[CLAUDE] New session for chat {chat_id} "
                f"({len(history)} messages)",
                file=sys.stderr,
            )

        result = await _run_with_fallback(
            cfg,
            chat_id=chat_id,
            primary_prompt=prompt,
            primary_resume_session_id=resume_session_id,
            fallback_prompt=fresh_prompt,
        )

        if not result.success:
            print(
                f"[CLAUDE] CLI failed after fallback chain for chat {chat_id} "
                f"(session={result.session_id}, resume={result.resumed}): "
                f"{result.error}",
                file=sys.stderr,
            )

        if result.success and result.text:
            response = f"{result.text}\n\n{random_signature()}"
        elif result.success:
            response = "⚠️ Claude returned an empty response."
        else:
            response = f"⚠️ {result.error or 'Claude invocation failed'}"

        if result.success and result.session_id:
            newest_id = max((m.id for m in history), default=msg.id)
            try:
                sessions.save(
                    chat_id=chat_id,
                    session_id=result.session_id,
                    newest_msg_id=newest_id,
                    last_input_tokens=result.total_tokens,
                    # On the turn we just compacted on, set the gate so the
                    # very next turn can't trigger a second compaction even
                    # if it lands above the threshold. Any subsequent turn
                    # clears the gate (just_compacted=False) and the normal
                    # threshold check resumes.
                    just_compacted=compacted_now,
                )
            except Exception as e:
                print(
                    f"[CLAUDE] Failed to persist chat session for chat {chat_id}: {e!r}",
                    file=sys.stderr,
                )
                logger.exception("Failed to persist chat session for chat_id=%s", chat_id)

        try:
            await client.edit_message(chat_id, ack.id, response)
        except Exception as e:
            print(
                f"[CLAUDE] Failed to edit ack message {ack.id}, sending new: {e!r}",
                file=sys.stderr,
            )
            try:
                await client.send_message(chat_id, response, reply_to=msg.id)
            except Exception as e2:
                print(
                    f"[CLAUDE] Fallback send_message also failed for chat {chat_id}: {e2!r}",
                    file=sys.stderr,
                )
                logger.exception("Fallback send_message also failed")
    except Exception:
        print(
            f"[CLAUDE] Trigger pipeline crashed for chat {chat_id}",
            file=sys.stderr,
        )
        logger.exception("Trigger pipeline crashed")
        try:
            await client.edit_message(
                chat_id, ack.id, "⚠️ @claude crashed mid-thought (check mcp_errors.log)"
            )
        except Exception:
            pass


def register(
    client,
    cfg: TriggerConfig,
    sessions: ChatSessionStore,
    trusted: TrustedUserStore,
    media: MediaStore | None = None,
    messages: MessageStore | None = None,
    reactions: ReactionCacheStore | None = None,
    mcp=None,
) -> None:
    """Wire @claude trigger handlers onto the Telethon client. Caller is
    responsible for running an HTTP transport (see configure_http_transport).
    `mcp` is the FastMCP instance — required for media auto-download to
    register concrete FileResource per (chat, message) with the actual
    mime_type (so spawned Claude can render images / play audio natively
    instead of getting opaque octet-stream blobs)."""
    locks = ChatLockManager()
    handler = _build_event_handler(
        client, cfg, locks, sessions, trusted, media, messages, reactions, mcp
    )
    # incoming=None catches both incoming and outgoing — Steve can self-trigger.
    client.add_event_handler(handler, events.NewMessage(incoming=None))
    client.add_event_handler(handler, events.MessageEdited(incoming=None))
    media_state = (
        f"on (max={cfg.media_max_size_mb}MB, "
        f"types={','.join(sorted(cfg.media_types)) or 'none'}, "
        f"dir={cfg.media_dir})"
        if (media is not None and cfg.media_auto_download_enabled)
        else "off"
    )
    edits_state = "on" if (messages is not None and cfg.show_edit_history) else "off"
    rxc_state = (
        f"on (ttl={cfg.reaction_cache_ttl_seconds}s)"
        if reactions is not None and cfg.reaction_cache_ttl_seconds > 0
        else "off"
    )
    print(
        f"[CLAUDE] Trigger registered (owner={cfg.owner_user_id}, "
        f"port={cfg.mcp_port}, vault={cfg.memory_vault or 'none'}, "
        f"db={cfg.db_path}, media={media_state}, edits={edits_state}, "
        f"reaction_cache={rxc_state})",
        file=sys.stderr,
    )


def configure_http_transport(mcp, cfg: TriggerConfig) -> None:
    """Mutate FastMCP settings so it serves the streamable-http transport on
    [::]:<port>/mcp/<api_key> (dual-stack v4+v6, matching whatsapp-mcp).
    Caller awaits mcp.run_streamable_http_async()."""
    # Bind IPv4 wildcard. We tried `::` for dual-stack but Python sockets on
    # Windows default to IPV6_V6ONLY=1, which blocks IPv4 clients on a v6
    # bind. Whatsapp-mcp (Go) gets dual-stack for free; uvicorn does not.
    # Clients should connect via 127.0.0.1, NOT localhost (which Windows tends
    # to resolve to ::1 first and won't fall back).
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = cfg.mcp_port
    mcp.settings.streamable_http_path = f"/mcp/{cfg.mcp_api_key}"
    # Return plain JSON instead of SSE-framed responses — Claude Code's MCP
    # client (and mcp-go-based servers like whatsapp-mcp) speak this dialect.
    mcp.settings.json_response = True
    # Stateless mode — required for Claude Code compatibility. After POST
    # initialize succeeds, Claude Code does GET /mcp/<key> to open the SSE
    # stream WITHOUT including the Mcp-Session-Id header from the init
    # response. FastMCP in stateful mode rejects that GET with 400 "Missing
    # session ID", causing the silent reconnect failure. Stateless mode
    # accepts the GET and matches mcp-go's default behavior (which is what
    # whatsapp-mcp uses).
    mcp.settings.stateless_http = True
    print(
        f"[telegram-mcp] HTTP daemon listening on http://0.0.0.0:{cfg.mcp_port}/mcp/<api_key>",
        file=sys.stderr,
    )
