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
- The primary database is AIRBNB (schema DBT_SCHEMA, warehouse COMPUTE_WH).
- SCOPE: you only help with this Snowflake account and its data (databases, schemas, \
tables, SQL, results). If asked about anything unrelated — news, sports, general \
knowledge, coding help unrelated to their data, etc. — politely decline in one sentence \
and steer back to their Snowflake data. Do not answer the off-topic question."""


# Pre-flight scope filter. A cheap, warm Haiku classifier decides whether a prompt is
# about the user's Snowflake data before we spend a full (multi-second) agent turn on it.
# This is the hard backstop; the SCOPE rule in SYSTEM_PROMPT above is defense-in-depth.
CLASSIFIER_PROMPT = (
    "You are a scope filter for a Snowflake data assistant. Reply with EXACTLY one word: "
    "ALLOW or BLOCK.\n"
    "Default to ALLOW. Only BLOCK a message that is CLEARLY about an unrelated topic with no "
    "plausible connection to a data warehouse.\n"
    "ALLOW = anything about the user's data or data platform: Snowflake, databases, schemas, "
    "tables, views, columns, SQL/queries, warehouses, roles; analytics and data-engineering "
    "concepts realized in the warehouse (dbt models, marts, staging models, sources, seeds, "
    "snapshots, lineage, pipelines, metrics); exploring/among/counting/describing objects; "
    "and normal conversational messages to the assistant (greetings, thanks, follow-ups like "
    "'and the next 5?'). If a message plausibly refers to their data, ALLOW it.\n"
    "BLOCK = only clearly off-topic subjects: news, sports, weather, politics, celebrities, "
    "general trivia, personal/medical/legal advice, or coding help unrelated to querying "
    "their data. When unsure, ALLOW. Output only ALLOW or BLOCK."
)

OFF_TOPIC_REPLY = (
    "I can only help with questions about your Snowflake account and its data — exploring "
    "databases, schemas and tables, or writing read-only SQL. Ask me something about your "
    "data and I'll dig in."
)


def make_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        # Disable the CLI "tool search" feature. Otherwise the MCP tools are
        # deferred behind the server-side ToolSearch tool: the model has to
        # discover them by name before it can call them, and a natural-language
        # question ("what tables are in AIRBNB?") makes it search by keyword,
        # find nothing, and wrongly report the Snowflake tools as unavailable.
        # With tool search off, all 7 mcp__snowflake__* tools are offered directly.
        env={"ENABLE_TOOL_SEARCH": "0"},
        # Lock the toolset down to Snowflake only. tools=[] disables every built-in
        # Claude Code tool (Bash, web, file edit, …) that bypassPermissions would
        # otherwise expose; the MCP tools are added on top and remain reachable
        # (verified) because tool search is off, so they are offered directly.
        tools=[],
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


def classifier_options() -> ClaudeAgentOptions:
    # Minimal, tool-less Haiku engine used only for the yes/no scope verdict.
    return ClaudeAgentOptions(
        system_prompt=CLASSIFIER_PROMPT,
        tools=[],
        permission_mode="bypassPermissions",
        setting_sources=[],
        cwd=str(ROOT),
        model="claude-haiku-4-5-20251001",
    )


# --- scope filter --------------------------------------------------------------

class ScopeClassifier:
    """One warm, shared Haiku client that judges each prompt in ~1s. Fails OPEN:
    if the classifier errors, the message is allowed (the SYSTEM_PROMPT scope rule
    is still a backstop) so an infra hiccup never blocks a legitimate user."""

    MAX_USES = 100  # recycle the client periodically to bound conversation growth

    def __init__(self) -> None:
        self.client: ClaudeSDKClient | None = None
        self.lock = asyncio.Lock()
        self.uses = 0

    async def _ensure(self) -> None:
        if self.client is None:
            self.client = ClaudeSDKClient(classifier_options())
            await self.client.connect()
            self.uses = 0

    async def _reset(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None

    async def allow(self, prompt: str) -> bool:
        async with self.lock:
            try:
                await self._ensure()
                assert self.client is not None
                await self.client.query(f"Classify ONLY this message: {prompt}")
                verdict = ""
                async for msg in self.client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                verdict += block.text
                self.uses += 1
                if self.uses >= self.MAX_USES:
                    await self._reset()
                return "BLOCK" not in verdict.strip().upper()
            except Exception:
                await self._reset()
                return True  # fail open


scope = ScopeClassifier()


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
    # Pre-flight scope gate: reject off-topic prompts before spending an agent turn.
    if not await scope.allow(prompt):
        yield sse({"type": "text", "text": OFF_TOPIC_REPLY})
        yield sse({"type": "done", "duration_ms": int((time.monotonic() - start) * 1000)})
        return
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
