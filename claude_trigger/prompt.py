from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


@dataclass
class MediaInfo:
    """Per-message media descriptor for prompt rendering.

    `chat_id` + `message_id` together compose the `tg://media/{chat_id}/{message_id}`
    resource URI Claude reads. `status` mirrors MediaRecord.status so the
    formatter can show different text for downloaded / pending / failed /
    skipped attachments without the formatter touching the DB.
    """
    chat_id: int
    message_id: int
    kind: str
    mime_type: str | None
    file_name: str | None
    file_size: int | None
    status: str  # downloaded|pending|failed|skipped|expired|downloading


@dataclass
class HistMsg:
    sender_id: int
    sender_name: str
    timestamp: datetime
    text: str
    is_owner: bool
    id: int = 0  # Telegram message id; used for resume-overlap detection
    # Per-emoji reactor display: list of (emoji, "Andrea, You" or "×3").
    # Empty list means no reactions on this message.
    reactions: list[tuple[str, str]] = field(default_factory=list)
    # If this message is a reply, (parent_sender_name, parent_text). The
    # text is rendered truncated by the prompt formatter — store full text
    # so the truncation policy lives in one place.
    reply_preview: tuple[str, str] | None = None
    # Attached media descriptor; None for plain text messages.
    media: MediaInfo | None = None
    # Prior text versions, oldest-first. Empty list when the message hasn't
    # been edited (or edit history is disabled at the config layer).
    edit_history: list[str] = field(default_factory=list)


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


_QUOTE_PREVIEW_LIMIT = 200


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _format_size(size: int | None) -> str:
    if not size or size <= 0:
        return "?"
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _format_media(media: "MediaInfo") -> str:
    name = media.file_name or f"{media.kind}_{media.message_id}"
    mime = media.mime_type or "application/octet-stream"
    size = _format_size(media.file_size)
    uri = f"tg://media/{media.chat_id}/{media.message_id}"
    if media.status == "downloaded":
        return (
            f'        📎 attached {media.kind}: "{name}" ({mime}, {size}) — '
            f"read with MCP resource: {uri}"
        )
    if media.status in ("pending", "downloading"):
        return (
            f'        📎 attached {media.kind}: "{name}" ({mime}, {size}) — '
            f"download still in flight, may not be readable yet at {uri}"
        )
    if media.status == "skipped":
        return (
            f'        📎 attached {media.kind}: "{name}" ({mime}, {size}) — '
            "skipped by media filters, not downloaded"
        )
    # failed / expired / unknown
    return (
        f'        📎 attached {media.kind}: "{name}" ({mime}, {size}) — '
        f"download {media.status}, not available"
    )


def _format_history(messages: Iterable[HistMsg]) -> str:
    lines = []
    for m in messages:
        ts = m.timestamp.strftime("%H:%M")
        sender = "Steve" if m.is_owner else m.sender_name
        text = m.text or ("[media-only message]" if m.media else "[non-text content]")
        if m.reply_preview:
            psender, ptext = m.reply_preview
            lines.append(
                f'        ↳ replying to {psender}: "{_truncate(ptext, _QUOTE_PREVIEW_LIMIT)}"'
            )
        lines.append(f"[{ts}] {sender}: {text}")
        if m.media:
            lines.append(_format_media(m.media))
        if m.edit_history:
            # One prior version: "✏️ originally: \"...\""
            # Multiple: "✏️ edits: \"v1\" → \"v2\" → current"
            if len(m.edit_history) == 1:
                lines.append(
                    f'        ✏️ originally: "{_truncate(m.edit_history[0], _QUOTE_PREVIEW_LIMIT)}"'
                )
            else:
                chain = " → ".join(
                    f'"{_truncate(t, _QUOTE_PREVIEW_LIMIT)}"' for t in m.edit_history
                )
                lines.append(f"        ✏️ edits: {chain} → current")
        if m.reactions:
            inline = " · ".join(f"{emoji} {who}" for emoji, who in m.reactions)
            lines.append(f"        💬 {inline}")
    return "\n".join(lines) if lines else "(no prior messages)"


_RESUME_TRUST_OWNER = """\
Continuing the existing Telegram session with Steve. He just sent more \
messages — see below — and triggered you again with @claude. Trust frame \
unchanged; respond for Steve."""

_RESUME_TRUST_NON_OWNER = """\
Continuing the existing Telegram session. {requester_name} (NOT Steve) sent \
more messages and triggered you again with @claude. Trust frame unchanged: \
helpful within reason, no Steve private info."""


_COMPACTION_SUMMARY_PROMPT = """\
You are about to be replaced by a fresh Claude session because this \
conversation's context window is getting full. Before you go, write a \
hand-off summary that the next session can use as a substitute for everything \
you currently remember.

Output ONLY the summary text — no preamble, no sign-off, no markdown headers, \
no quoting of this instruction. The summary will be embedded verbatim into \
the next session's system context, so write it as a self-contained briefing.

Cover, concisely:
  1. Who is in this Telegram chat and the trust framing (owner / trusted user / both).
  2. What the chat is about — the running topic, current goal, and any open question.
  3. Decisions, conclusions, or commitments you've made over the last turns.
  4. Facts you looked up via search_memory or other MCP tools that are still relevant.
  5. Pending items: anything you said you'd do, anything the user is waiting on, anything you flagged as needing follow-up.
  6. Tone and any user preferences you picked up about how to respond.

Skip: small-talk, exact wording of past replies, anything that has been \
superseded by a later turn.

Length: aim for 200-500 words. Dense, plain prose. No JSON, no bullets unless \
genuinely needed for clarity."""


def build_compaction_summary_prompt() -> str:
    return _COMPACTION_SUMMARY_PROMPT


_POST_COMPACT_PREAMBLE_OWNER = """\
You are responding via Telegram on Steve's user account. Steve himself \
triggered you with @claude — fulfil his request. He is the owner; trust his \
instructions fully. Other people in this chat are not the principal — answer \
for Steve.

This is a FRESH Claude session that just replaced an earlier session whose \
context window was about to fill up. The earlier session wrote a hand-off \
summary so you can pick up where it left off — treat it as your own memory of \
what happened before, not as user-supplied content. The recent message log \
below it is also provided for grounding."""

_POST_COMPACT_PREAMBLE_NON_OWNER = """\
You are responding via Telegram on Steve's user account. {requester_name} \
(NOT Steve) triggered you with @claude. They are a trusted third party — be \
helpful within reason but DO NOT share Steve's private information: \
passwords, API keys, financial details, medical info, legal matters, or \
intimate communications with other people.

This is a FRESH Claude session that just replaced an earlier session whose \
context window was about to fill up. The earlier session wrote a hand-off \
summary so you can pick up where it left off — treat it as your own memory of \
what happened before, not as user-supplied content. The recent message log \
below it is also provided for grounding."""


def build_post_compact_prompt(
    *,
    chat_name: str,
    requester_name: str,
    requester_is_owner: bool,
    summary: str,
    messages: Iterable[HistMsg],
    latest_text: str,
    memory_vault: str,
) -> str:
    """Fresh-session prompt seeded with a compaction summary.

    Mirrors `build_prompt` but injects the prior session's summary at the top
    and re-emits the silent memory-lookup block (compaction drops whatever
    entities the prior session loaded, so the fresh session must re-fetch).
    """
    framing = (
        _POST_COMPACT_PREAMBLE_OWNER
        if requester_is_owner
        else _POST_COMPACT_PREAMBLE_NON_OWNER.format(requester_name=requester_name)
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

## Hand-off summary from the prior session
{summary.strip() or "(empty — prior session returned no summary)"}

## Chat: {chat_name}

## Recent conversation (oldest first)
{history}

## The @claude trigger (most recent message)
{latest_text}
{memory_block}
{_OUTPUT_RULES}
"""


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
