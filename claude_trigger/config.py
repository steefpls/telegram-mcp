import os
from dataclasses import dataclass
from typing import Optional


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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
        )
