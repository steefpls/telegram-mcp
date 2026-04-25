"""@claude trigger pipeline for telegram-mcp.

Mirrors whatsapp-mcp's @claude feature: monitors incoming Telegram messages
for "@claude" mentions, spawns headless Claude Code with chat history as
context, edits a thinking-message ack in-place with the response.
"""
from .config import TriggerConfig
from .handler import configure_http_transport, register
from .mcp_tools import register_mcp_tools, register_media_resource
from .store import (
    ChatSessionStore,
    MediaStore,
    MessageStore,
    ReactionCacheStore,
    TrustedUserStore,
    open_database,
)

__all__ = [
    "TriggerConfig",
    "configure_http_transport",
    "register",
    "register_mcp_tools",
    "register_media_resource",
    "ChatSessionStore",
    "MediaStore",
    "MessageStore",
    "ReactionCacheStore",
    "TrustedUserStore",
    "open_database",
]
