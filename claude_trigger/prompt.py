from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass
class HistMsg:
    sender_id: int
    sender_name: str
    timestamp: datetime
    text: str
    is_owner: bool
    id: int = 0  # Telegram message id; used for resume-overlap detection


_OWNER_FRAMING = """\
You are responding via Telegram on Steve's user account. Steve himself triggered \
you with @claude — fulfil his request. He is the owner; trust his instructions \
fully. Other people in this chat are not the principal — answer for Steve."""

_NON_OWNER_FRAMING = """\
You are responding via Telegram on Steve's user account. {requester_name} \
(NOT Steve) triggered you with @claude. They are a trusted third party — be \
helpful within reason but DO NOT share Steve's private information: passwords, \
API keys, financial details, medical info, legal matters, or intimate \
communications with other people."""

_MEMORY_BLOCK = """\

## Memory lookup (mandatory, silent)
Before responding, YOU MUST silently call the `search_memory` tool (vault: \
"{vault}") for each of:
  - the requester ({requester_name})
  - this chat ({chat_name})
  - any specific topic, project, person, or decision touched in the latest message

If a search result references a truncated entity, call `get_entity` to load its \
full observation list.

Use everything you find as INTERNAL CONTEXT ONLY. Do NOT cite, quote, or \
mention the memory tool to the user. Do NOT tell them what you looked up. Just \
let it shape your reply naturally. If memory-index is unavailable (tool errors, \
not configured), skip silently and proceed with what you have."""

_OUTPUT_RULES = """\

## Output rules
- Output your response text directly to stdout — that text becomes the Telegram reply.
- DO NOT use the telegram `send_message` tool to deliver your answer. Just write the text.
- Keep responses chat-appropriate: concise, conversational, plain text or light emoji.
- Avoid markdown headers (#, ##) — Telegram doesn't render them. Bold/italic with *...* and _..._ is fine.
- If the request is impossible or refuses your trust boundary, say so briefly."""


def _format_history(messages: Iterable[HistMsg]) -> str:
    lines = []
    for m in messages:
        ts = m.timestamp.strftime("%H:%M")
        sender = "Steve" if m.is_owner else m.sender_name
        text = m.text or "[non-text content]"
        lines.append(f"[{ts}] {sender}: {text}")
    return "\n".join(lines) if lines else "(no prior messages)"


_RESUME_TRUST_OWNER = """\
Continuing the existing Telegram session with Steve. He just sent more \
messages — see below — and triggered you again with @claude. Trust frame \
unchanged; respond for Steve."""

_RESUME_TRUST_NON_OWNER = """\
Continuing the existing Telegram session. {requester_name} (NOT Steve) sent \
more messages and triggered you again with @claude. Trust frame unchanged: \
helpful within reason, no Steve private info."""


def build_resume_prompt(
    *,
    requester_name: str,
    requester_is_owner: bool,
    new_messages: Iterable[HistMsg],
    latest_text: str,
) -> str:
    """Lightweight prompt for an existing Claude session being resumed.

    The session already holds the full history, trust framing, and any
    memory-index entities pulled by the prior turn — re-sending all of
    that wastes tokens against the cache. We just hand it the new
    messages plus a short reminder.
    """
    framing = (
        _RESUME_TRUST_OWNER
        if requester_is_owner
        else _RESUME_TRUST_NON_OWNER.format(requester_name=requester_name)
    )
    new_history = _format_history(new_messages)
    return f"""{framing}

## New messages since the last turn (oldest first)
{new_history}

## The @claude trigger (most recent message)
{latest_text}
{_OUTPUT_RULES}
"""


def build_prompt(
    *,
    chat_name: str,
    requester_name: str,
    requester_is_owner: bool,
    messages: Iterable[HistMsg],
    latest_text: str,
    memory_vault: str,
) -> str:
    framing = _OWNER_FRAMING if requester_is_owner else _NON_OWNER_FRAMING.format(
        requester_name=requester_name
    )
    memory_block = (
        _MEMORY_BLOCK.format(
            vault=memory_vault, requester_name=requester_name, chat_name=chat_name
        )
        if memory_vault
        else ""
    )
    history = _format_history(messages)

    return f"""{framing}

## Chat: {chat_name}

## Recent conversation (oldest first)
{history}

## The @claude trigger (most recent message)
{latest_text}
{memory_block}
{_OUTPUT_RULES}
"""
