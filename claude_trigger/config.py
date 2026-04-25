import os
from dataclasses import dataclass, field
from typing import Optional


# Telethon media kinds we're willing to auto-download. "document" covers any
# DocumentAttribute file that doesn't match a more specific kind (audio, video,
# voice, video_note, gif, sticker). Webpages and contacts are intentionally
# excluded — no useful bytes for the model to read.
_KNOWN_MEDIA_KINDS = frozenset({
    "photo", "voice", "audio", "video", "video_note", "gif", "sticker", "document",
})

# Default set if MEDIA_AUTO_DOWNLOAD_TYPES is not set. Skips stickers (noisy in
# group chats, low information density) and large unspecified documents.
_DEFAULT_MEDIA_TYPES = "photo,voice,audio,video,video_note,gif,document"


def _truthy(value: Optional[str], default: bool = False) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_media_types(raw: str) -> frozenset[str]:
    parts = [p.strip().lower() for p in raw.split(",")]
    parts = [p for p in parts if p]
    unknown = [p for p in parts if p not in _KNOWN_MEDIA_KINDS]
    if unknown:
        raise RuntimeError(
            f"MEDIA_AUTO_DOWNLOAD_TYPES has unknown kinds: {unknown}. "
            f"Allowed: {sorted(_KNOWN_MEDIA_KINDS)}"
        )
    return frozenset(parts)


@dataclass(frozen=True)
class TriggerConfig:
    claude_path: str
    claude_model: str
    max_budget_usd: str
    timeout_seconds: int
    mcp_port: int
    mcp_api_key: str
    owner_user_id: int
    memory_vault: str
    history_messages: int
    db_path: str
    session_ttl_hours: int
    compact_threshold_tokens: int
    compact_model: str
    compact_timeout_seconds: int
    compact_max_budget_usd: str
    media_auto_download_enabled: bool
    media_max_size_mb: int
    media_types: frozenset[str] = field(default_factory=frozenset)
    media_dir: str = "data/media"
    media_wait_timeout_seconds: float = 10.0
    show_edit_history: bool = True
    reaction_cache_ttl_seconds: int = 300

    @classmethod
    def from_env(cls) -> Optional["TriggerConfig"]:
        if not _truthy(os.getenv("CLAUDE_TRIGGER_ENABLED")):
            return None

        owner = os.getenv("OWNER_USER_ID", "").strip()
        if not owner:
            raise RuntimeError(
                "CLAUDE_TRIGGER_ENABLED=true requires OWNER_USER_ID (your Telegram user_id)"
            )
        api_key = os.getenv("MCP_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "CLAUDE_TRIGGER_ENABLED=true requires MCP_API_KEY (any random secret string)"
            )
        if "/" in api_key or " " in api_key:
            raise RuntimeError("MCP_API_KEY must not contain '/' or whitespace")

        return cls(
            claude_path=os.getenv("CLAUDE_PATH", "claude"),
            claude_model=os.getenv("CLAUDE_MODEL", "").strip(),
            max_budget_usd=os.getenv("CLAUDE_MAX_BUDGET_USD", "1.00").strip(),
            timeout_seconds=int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "300")),
            mcp_port=int(os.getenv("MCP_PORT", "8081")),
            mcp_api_key=api_key,
            owner_user_id=int(owner),
            memory_vault=os.getenv("MEMORY_VAULT", "").strip(),
            history_messages=int(os.getenv("CLAUDE_HISTORY_MESSAGES", "40")),
            db_path=os.getenv("CLAUDE_TRIGGER_DB_PATH", "data/telegram_mcp.db").strip(),
            session_ttl_hours=int(os.getenv("CLAUDE_SESSION_TTL_HOURS", "24")),
            compact_threshold_tokens=int(
                os.getenv("CLAUDE_COMPACT_THRESHOLD_TOKENS", "120000")
            ),
            compact_model=os.getenv("CLAUDE_COMPACT_MODEL", "haiku").strip(),
            compact_timeout_seconds=int(
                os.getenv("CLAUDE_COMPACT_TIMEOUT_SECONDS", "180")
            ),
            compact_max_budget_usd=os.getenv(
                "CLAUDE_COMPACT_MAX_BUDGET_USD", "0.50"
            ).strip(),
            # Default ON when the trigger is enabled — auto-downloading media
            # is the whole point of having a 24/7 daemon. Operator can disable
            # explicitly with MEDIA_AUTO_DOWNLOAD_ENABLED=false.
            media_auto_download_enabled=_truthy(
                os.getenv("MEDIA_AUTO_DOWNLOAD_ENABLED"), default=True
            ),
            media_max_size_mb=int(os.getenv("MEDIA_AUTO_DOWNLOAD_MAX_SIZE_MB", "20")),
            media_types=_parse_media_types(
                os.getenv("MEDIA_AUTO_DOWNLOAD_TYPES", _DEFAULT_MEDIA_TYPES)
            ),
            media_dir=os.getenv("MEDIA_DOWNLOAD_DIR", "data/media").strip(),
            # Tight upper bound — the user is staring at "thinking..." in
            # the chat while we wait. 10s is enough for typical photos /
            # voice notes; oversized docs should already be `skipped`.
            media_wait_timeout_seconds=float(
                os.getenv("MEDIA_WAIT_TIMEOUT_SECONDS", "10")
            ),
            # Default ON — Steve enabled this knowing the privacy trade-off.
            # Surfaces "originally said X, edited to Y" in the prompt so the
            # model has the full conversational signal that Telegram's bare
            # "(edited)" marker only hints at. Disable in shared chats where
            # other participants edit and reasonably expect the prior text
            # to stay private.
            show_edit_history=_truthy(
                os.getenv("CLAUDE_SHOW_EDIT_HISTORY"), default=True
            ),
            # Per-message reaction-list TTL. 5 minutes is conservative —
            # most reactions arrive in a burst right after the message and
            # stabilize quickly. Longer TTL = fewer API calls; shorter =
            # faster surfacing of late reactions.
            reaction_cache_ttl_seconds=int(
                os.getenv("CLAUDE_REACTION_CACHE_TTL_SECONDS", "300")
            ),
        )
