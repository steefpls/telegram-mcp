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
