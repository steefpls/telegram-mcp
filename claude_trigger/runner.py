import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass

logger = logging.getLogger("telegram_mcp.claude_trigger.runner")


@dataclass
class ClaudeResult:
    success: bool
    text: str
    cost_usd: float = 0.0
    num_turns: int = 0
    total_tokens: int = 0
    error: str = ""
    session_id: str = ""  # the uuid passed via --session-id or --resume
    resumed: bool = False


def _make_mcp_config(port: int, api_key: str) -> str:
    cfg = {
        "mcpServers": {
            "telegram-mcp": {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/mcp/{api_key}",
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="claude-mcp-tg-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


async def run_claude(
    prompt: str,
    *,
    claude_path: str,
    model: str,
    max_budget_usd: str,
    timeout_seconds: int,
    mcp_port: int,
    mcp_api_key: str,
    resume_session_id: str = "",
) -> ClaudeResult:
    if resume_session_id:
        session_id = resume_session_id
        session_flag = "--resume"
        resumed = True
    else:
        session_id = str(uuid.uuid4())
        session_flag = "--session-id"
        resumed = False
    config_path = _make_mcp_config(mcp_port, mcp_api_key)
    args = [
        claude_path,
        "--print",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        session_flag,
        session_id,
        "--mcp-config",
        config_path,
    ]
    if model:
        args += ["--model", model]
    if max_budget_usd:
        args += ["--max-budget-usd", max_budget_usd]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    print(
        f"[CLAUDE] Spawning: {claude_path} "
        f"(model={model}, session={session_id}, resume={str(resumed).lower()})",
        file=sys.stderr,
    )

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return ClaudeResult(
                success=False,
                text="",
                error=f"Claude CLI timed out after {timeout_seconds}s",
                session_id=session_id,
                resumed=resumed,
            )
    except FileNotFoundError:
        return ClaudeResult(
            success=False,
            text="",
            error=f"Claude CLI not found at '{claude_path}' (set CLAUDE_PATH)",
            session_id=session_id,
            resumed=resumed,
        )
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        snippet = (stderr or stdout)[-500:].strip()
        return ClaudeResult(
            success=False,
            text="",
            error=f"Claude CLI exit {proc.returncode}: {snippet}",
            session_id=session_id,
            resumed=resumed,
        )

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        print(
            f"[CLAUDE] Failed to parse JSON output (falling back to raw): "
            f"first 200 chars: {stdout[:200]!r}",
            file=sys.stderr,
        )
        return ClaudeResult(
            success=True,
            text=stdout.strip(),
            session_id=session_id,
            resumed=resumed,
        )

    text = (data.get("result") or "").strip()
    usage = data.get("usage") or {}
    input_t = int(usage.get("input_tokens", 0) or 0)
    cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    output_t = int(usage.get("output_tokens", 0) or 0)
    num_turns = max(int(data.get("num_turns", 1) or 1), 1)
    raw_total = input_t + cache_create + cache_read
    est_context = raw_total // num_turns
    cost = float(data.get("total_cost_usd", 0.0) or 0.0)

    print(
        f"[CLAUDE] Turn complete: num_turns={num_turns} input={input_t} "
        f"cache_create={cache_create} cache_read={cache_read} "
        f"raw_total={raw_total} est_context={est_context} output={output_t} "
        f"cost=${cost:.4f}",
        file=sys.stderr,
    )

    return ClaudeResult(
        success=True,
        text=text,
        cost_usd=cost,
        num_turns=num_turns,
        total_tokens=est_context,
        session_id=session_id,
        resumed=resumed,
    )
