import asyncio
import logging
import sys
from datetime import datetime, timezone

from telethon import events
from telethon.tl.custom import Message

from .config import TriggerConfig
from .locks import ChatLockManager
from .messages import (
    is_claude_response,
    is_thinking_message,
    random_signature,
    random_thinking,
)
from .prompt import HistMsg, build_prompt
from .runner import run_claude

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
    out: list[HistMsg] = []
    async for m in client.iter_messages(chat_id, limit=limit):
        text = m.text or ""
        if not text:
            continue
        if is_thinking_message(text):
            continue
        sender_id = m.sender_id or 0
        is_owner = sender_id == owner_id
        sender_name = "Steve" if is_owner else await _resolve_sender_name(client, sender_id)
        ts = m.date or datetime.now(timezone.utc)
        out.append(
            HistMsg(
                sender_id=sender_id,
                sender_name=sender_name,
                timestamp=ts,
                text=text,
                is_owner=is_owner,
            )
        )
    out.reverse()  # oldest first
    return out


def _build_event_handler(client, cfg: TriggerConfig, locks: ChatLockManager):
    async def handler(event):
        try:
            await _maybe_dispatch(client, cfg, locks, event)
        except Exception:
            logger.exception("Unhandled error in @claude event handler")

    return handler


async def _maybe_dispatch(
    client, cfg: TriggerConfig, locks: ChatLockManager, event
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
    if sender_id != cfg.owner_user_id:
        # MVP: owner-only. Phase 2 adds trusted_users table.
        logger.info(
            "Ignoring @claude from non-owner sender_id=%s (owner=%s)",
            sender_id,
            cfg.owner_user_id,
        )
        return

    chat_id = msg.chat_id
    lock = locks.get(chat_id)
    async with lock:
        await _process_trigger(client, cfg, msg, chat_id, sender_id)


async def _process_trigger(
    client, cfg: TriggerConfig, msg: Message, chat_id: int, sender_id: int
) -> None:
    # Send thinking ack as a reply to the trigger message
    try:
        ack = await client.send_message(chat_id, random_thinking(), reply_to=msg.id)
    except Exception:
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

        prompt = build_prompt(
            chat_name=chat_name,
            requester_name=requester_name,
            requester_is_owner=requester_is_owner,
            messages=history,
            latest_text=msg.text or "",
            memory_vault=cfg.memory_vault,
        )

        print(
            f"[@claude] dispatch chat={chat_id} sender={sender_id} prompt_len={len(prompt)}",
            file=sys.stderr,
        )

        result = await run_claude(
            prompt,
            claude_path=cfg.claude_path,
            model=cfg.claude_model,
            max_budget_usd=cfg.max_budget_usd,
            timeout_seconds=cfg.timeout_seconds,
            mcp_port=cfg.mcp_port,
            mcp_api_key=cfg.mcp_api_key,
        )

        if result.success and result.text:
            response = f"{result.text}\n\n{random_signature()}"
        elif result.success:
            response = "⚠️ Claude returned an empty response."
        else:
            response = f"⚠️ {result.error or 'Claude invocation failed'}"

        print(
            f"[@claude] result chat={chat_id} ok={result.success} "
            f"tokens={result.total_tokens} cost=${result.cost_usd:.4f} "
            f"len={len(result.text)}",
            file=sys.stderr,
        )

        try:
            await client.edit_message(chat_id, ack.id, response)
        except Exception:
            logger.exception("Edit-in-place failed; sending fresh message")
            try:
                await client.send_message(chat_id, response, reply_to=msg.id)
            except Exception:
                logger.exception("Fallback send_message also failed")
    except Exception:
        logger.exception("Trigger pipeline crashed")
        try:
            await client.edit_message(
                chat_id, ack.id, "⚠️ @claude crashed mid-thought (check mcp_errors.log)"
            )
        except Exception:
            pass


def register(client, cfg: TriggerConfig) -> None:
    """Wire @claude trigger handlers onto the Telethon client. Caller is
    responsible for running an HTTP transport (see configure_http_transport)."""
    locks = ChatLockManager()
    handler = _build_event_handler(client, cfg, locks)
    # incoming=None catches both incoming and outgoing — Steve can self-trigger.
    client.add_event_handler(handler, events.NewMessage(incoming=None))
    client.add_event_handler(handler, events.MessageEdited(incoming=None))
    print(
        f"[@claude] trigger registered (owner={cfg.owner_user_id}, "
        f"port={cfg.mcp_port}, vault={cfg.memory_vault or 'none'})",
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
