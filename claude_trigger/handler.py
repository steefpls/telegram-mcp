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
from .messages import (
    is_claude_response,
    is_thinking_message,
    random_signature,
    random_thinking,
)
from .prompt import (
    HistMsg,
    build_compaction_summary_prompt,
    build_post_compact_prompt,
    build_prompt,
    build_resume_prompt,
)
from .runner import ClaudeResult, run_claude
from .store import ChatSessionStore, TrustedUserStore

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
    client, chat_id: int, limit: int, owner_id: int
) -> list[HistMsg]:
    raw: list[Message] = []
    async for m in client.iter_messages(chat_id, limit=limit):
        text = m.text or ""
        if not text:
            continue
        if is_thinking_message(text):
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

    # --- Per-message reactor lookups (parallel) ---
    async def fetch_reactions_for(m: Message) -> list[tuple[str, str]]:
        if not getattr(m, "reactions", None):
            return []
        # Rich path: resolve each reactor's name. limit=20 is plenty for
        # group chats — beyond that we render a count instead.
        try:
            res = await client(
                GetMessageReactionsListRequest(peer=chat_id, id=m.id, limit=20)
            )
        except Exception:
            res = None
        if res is not None and getattr(res, "reactions", None):
            grouped: dict[str, list[int]] = {}
            for pr in res.reactions:
                emoji = getattr(pr.reaction, "emoticon", None) or "?"
                uid = getattr(pr.peer_id, "user_id", None) or 0
                grouped.setdefault(emoji, []).append(uid)
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
):
    async def handler(event):
        try:
            await _maybe_dispatch(client, cfg, locks, sessions, trusted, event)
        except Exception:
            logger.exception("Unhandled error in @claude event handler")

    return handler


async def _maybe_dispatch(
    client,
    cfg: TriggerConfig,
    locks: ChatLockManager,
    sessions: ChatSessionStore,
    trusted: TrustedUserStore,
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
        await _process_trigger(client, cfg, sessions, msg, chat_id, sender_id)


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
        history = await _gather_history(
            client, chat_id, cfg.history_messages, cfg.owner_user_id
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
) -> None:
    """Wire @claude trigger handlers onto the Telethon client. Caller is
    responsible for running an HTTP transport (see configure_http_transport)."""
    locks = ChatLockManager()
    handler = _build_event_handler(client, cfg, locks, sessions, trusted)
    # incoming=None catches both incoming and outgoing — Steve can self-trigger.
    client.add_event_handler(handler, events.NewMessage(incoming=None))
    client.add_event_handler(handler, events.MessageEdited(incoming=None))
    print(
        f"[CLAUDE] Trigger registered (owner={cfg.owner_user_id}, "
        f"port={cfg.mcp_port}, vault={cfg.memory_vault or 'none'}, "
        f"db={cfg.db_path})",
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
