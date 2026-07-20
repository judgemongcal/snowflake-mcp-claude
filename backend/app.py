"""
FastAPI backend for the Snowflake chat app.

Path B architecture: this backend uses the Claude Agent SDK, which runs the same
engine as Claude Code using the machine's existing Claude Code login (no separate
API key). It connects to our local, read-only Snowflake MCP server (../server.py)
and streams Claude's reasoning + tool calls + answer to the frontend over SSE.

Endpoints:
  GET  /api/health           preflight: files + Claude engine + MCP status
  POST /api/chat  {prompt, session}   -> text/event-stream of turn events
  POST /api/reset {session}           -> clears a conversation
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

ROOT = Path(__file__).resolve().parent.parent          # mcp-sf/
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
SERVER_PY = ROOT / "server.py"
ENV_FILE = ROOT / ".env"
DIST = ROOT / "frontend" / "dist"

SNOWFLAKE_TOOLS = [
    "mcp__snowflake__get_connection_info",
    "mcp__snowflake__execute_query",
    "mcp__snowflake__list_databases",
    "mcp__snowflake__list_schemas",
    "mcp__snowflake__list_tables",
    "mcp__snowflake__describe_table",
    "mcp__snowflake__preview_table",
]

SYSTEM_PROMPT = """You are a senior data engineer assistant connected to the user's \
Snowflake account through MCP tools (mcp__snowflake__*).

- Use the tools to answer questions about their Snowflake warehouse: run \
SELECT/SHOW/DESCRIBE queries, list databases/schemas/tables, and describe or \
preview tables. Discover context before assuming a schema.
- The connection is currently READ-ONLY: writes and DDL (INSERT/UPDATE/DELETE/\
CREATE/DROP) are blocked by the server and will return an error. If asked to modify \
data, explain that it is read-only and what would need to change to enable it.
- Always present query results as clean GitHub-flavored markdown tables. Keep \
explanations concise.
- Qualify object names as DATABASE.SCHEMA.OBJECT. Confirm before anything destructive.
- The primary database is AIRBNB (schema DBT_SCHEMA, warehouse COMPUTE_WH)."""


def make_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={
            "snowflake": {
                "type": "stdio",
                "command": str(VENV_PY),
                "args": [str(SERVER_PY)],
            }
        },
        allowed_tools=SNOWFLAKE_TOOLS,
        permission_mode="bypassPermissions",
        strict_mcp_config=True,   # only our snowflake server; ignore other MCP config
        setting_sources=[],        # self-contained; don't load project/user settings
        cwd=str(ROOT),
        model="sonnet",
    )


# --- session management --------------------------------------------------------

class ChatSession:
    def __init__(self) -> None:
        self.client = ClaudeSDKClient(make_options())
        self.lock = asyncio.Lock()
        self.connected = False

    async def ensure(self) -> None:
        if not self.connected:
            await self.client.connect()
            self.connected = True

    async def close(self) -> None:
        if self.connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.connected = False


sessions: dict[str, ChatSession] = {}


def get_session(sid: str) -> ChatSession:
    s = sessions.get(sid)
    if s is None:
        s = ChatSession()
        sessions[sid] = s
    return s


# --- SSE helpers ---------------------------------------------------------------

def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


def _tool_result_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text", "")))
            else:
                parts.append(str(getattr(c, "text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def summarize_tool_result(block: ToolResultBlock) -> tuple[bool, str]:
    text = _tool_result_text(block)
    ok = not bool(getattr(block, "is_error", False))
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if "error" in data:
                return False, str(data["error"])[:200]
            if "row_count" in data:
                s = f"{data['row_count']} row(s)"
                if data.get("truncated"):
                    s += " (truncated)"
                return ok, s
            return ok, "ok"
    except Exception:
        pass
    return ok, (text[:120] + "…") if len(text) > 120 else (text or "ok")


async def stream_turn(session: ChatSession, prompt: str) -> AsyncIterator[str]:
    start = time.monotonic()
    async with session.lock:
        try:
            await session.ensure()
            await session.client.query(prompt)
            async for msg in session.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield sse({"type": "text", "text": block.text})
                        elif isinstance(block, ThinkingBlock):
                            yield sse({"type": "thinking", "text": block.thinking})
                        elif isinstance(block, ToolUseBlock):
                            yield sse({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            })
                elif isinstance(msg, UserMessage):
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                ok, summary = summarize_tool_result(block)
                                yield sse({
                                    "type": "tool_result",
                                    "id": block.tool_use_id,
                                    "ok": ok,
                                    "summary": summary,
                                })
                elif isinstance(msg, ResultMessage):
                    yield sse({
                        "type": "done",
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    })
        except CLINotFoundError as e:
            yield sse({"type": "error", "message": f"Claude Code engine not found: {e}"})
        except Exception as e:  # noqa: BLE001
            yield sse({"type": "error", "message": str(e)})


# --- app -----------------------------------------------------------------------

app = FastAPI(title="SnowChat")


@app.get("/api/health")
async def health() -> JSONResponse:
    checks = [
        {"name": "Snowflake MCP server", "ok": SERVER_PY.exists(), "detail": str(SERVER_PY)},
        {"name": "Connection config (.env)", "ok": ENV_FILE.exists(),
         "detail": "present" if ENV_FILE.exists() else "missing mcp-sf/.env"},
    ]
    engine_ok = False
    engine_detail = ""
    mcp_ok = False
    mcp_detail = ""
    try:
        probe = ClaudeSDKClient(make_options())
        await probe.connect()
        engine_ok = True
        engine_detail = "Claude Code engine reachable (using your login)"
        try:
            status = await probe.get_mcp_status()
            servers = getattr(status, "servers", None) or getattr(status, "mcp_servers", None) or []
            for srv in servers:
                name = getattr(srv, "name", None) or (srv.get("name") if isinstance(srv, dict) else None)
                if name == "snowflake":
                    st = getattr(srv, "status", None) or (srv.get("status") if isinstance(srv, dict) else None)
                    mcp_ok = str(st).lower().find("connect") >= 0 or str(st).lower() == "ok"
                    mcp_detail = f"status: {st}"
            if not mcp_detail:
                mcp_detail = "snowflake server not reported yet (starts on first use)"
                mcp_ok = True
        except Exception as e:  # noqa: BLE001
            mcp_detail = f"status unavailable: {e}"
            mcp_ok = True  # non-fatal; verified lazily on first query
        await probe.disconnect()
    except CLINotFoundError:
        engine_detail = "Claude Code CLI not found. Install Claude Code and run `claude` to log in."
    except Exception as e:  # noqa: BLE001
        engine_detail = f"{e}"
    checks.append({"name": "Claude Code engine + login", "ok": engine_ok, "detail": engine_detail})
    checks.append({"name": "Snowflake MCP connection", "ok": mcp_ok, "detail": mcp_detail})

    ready = all(c["ok"] for c in checks)
    message = "Ready." if ready else "Setup needed — see the checks below."
    return JSONResponse({"ready": ready, "checks": checks, "message": message})


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    sid = body.get("session") or "default"
    if not prompt:
        async def empty() -> AsyncIterator[str]:
            yield sse({"type": "error", "message": "empty prompt"})
            yield sse({"type": "done", "duration_ms": 0})
        return StreamingResponse(empty(), media_type="text/event-stream")

    session = get_session(sid)
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream_turn(session, prompt),
                             media_type="text/event-stream", headers=headers)


@app.post("/api/reset")
async def reset(request: Request) -> JSONResponse:
    body = await request.json()
    sid = body.get("session") or "default"
    session = sessions.pop(sid, None)
    if session:
        await session.close()
    return JSONResponse({"ok": True})


# Serve the built frontend (production). In dev, use the Vite dev server instead.
if DIST.exists():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="static")


if __name__ == "__main__":
    # Ensure subprocess-capable event loop on Windows (Agent SDK spawns the CLI).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
