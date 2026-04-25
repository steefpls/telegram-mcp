"""MCP tools for managing the @claude trusted-user allowlist.

Registered onto the FastMCP server only when the @claude trigger is
enabled. The owner (TriggerConfig.owner_user_id) is implicitly trusted
and is NOT stored in this table — these tools manage everyone else.
"""
from datetime import datetime, timezone

from mcp.types import ToolAnnotations

from .store import TrustedUserStore


def register_mcp_tools(mcp, store: TrustedUserStore) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Add Trusted User",
            openWorldHint=False,
            destructiveHint=False,
            idempotentHint=True,
        )
    )
    async def add_trusted_user(user_id: int, note: str = "") -> str:
        """
        Add a Telegram user_id to the @claude trigger allowlist. Trusted users
        can invoke `@claude` in any chat that telegram-mcp watches; without
        this, only the configured owner can trigger.

        user_id: numeric Telegram user_id (positive integer).
        note: optional free-text label, e.g. "Andrea (girlfriend)".
        """
        try:
            inserted = store.add(user_id, note=(note or None))
            return (
                f"Added user_id={user_id} to trusted_users."
                if inserted
                else f"user_id={user_id} was already trusted."
            )
        except ValueError as e:
            return f"Error: {e}"

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Remove Trusted User",
            openWorldHint=False,
            destructiveHint=True,
            idempotentHint=True,
        )
    )
    async def remove_trusted_user(user_id: int) -> str:
        """
        Remove a Telegram user_id from the @claude trigger allowlist.

        user_id: numeric Telegram user_id to revoke.
        """
        deleted = store.remove(user_id)
        return (
            f"Removed user_id={user_id} from trusted_users."
            if deleted
            else f"user_id={user_id} was not in trusted_users."
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List Trusted Users",
            openWorldHint=False,
            readOnlyHint=True,
        )
    )
    async def list_trusted_users() -> str:
        """List every user_id currently allowed to invoke @claude (excluding the owner)."""
        rows = store.list()
        if not rows:
            return "No trusted users configured. Owner-only @claude triggering."
        lines = []
        for r in rows:
            ts = datetime.fromtimestamp(r.added_at, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%MZ"
            )
            note = f" — {r.note}" if r.note else ""
            lines.append(f"  - {r.user_id}{note} (added {ts})")
        return "Trusted users:\n" + "\n".join(lines)
