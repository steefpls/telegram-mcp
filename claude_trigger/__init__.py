"""@claude trigger pipeline for telegram-mcp.

Mirrors whatsapp-mcp's @claude feature: monitors incoming Telegram messages
for "@claude" mentions, spawns headless Claude Code with chat history as
context, edits a thinking-message ack in-place with the response.
"""
from .config import TriggerConfig
from .handler import configure_http_transport, register

__all__ = ["TriggerConfig", "configure_http_transport", "register"]
