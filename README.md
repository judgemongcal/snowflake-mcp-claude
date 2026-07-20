# Snowflake Chat

A standalone, **Claude-like chat app for your Snowflake account**. You ask a question
in plain English; Claude discovers the schema, writes and runs **read-only** SQL through
a self-owned MCP server, and streams the answer back as GitHub-flavored markdown tables —
reasoning, tool calls, and results all visible as they happen.

No Cortex. No separate API key or bill (it reuses your existing Claude Code login).
The tool surface and the read-only safety gate are entirely under our control.

---

## 1. End-to-end architecture

```
┌───────────────┐   HTTP/SSE   ┌────────────────────┐   Agent SDK    ┌──────────────────┐
│  Browser UI   │ ───────────▶ │   FastAPI backend  │ ─────────────▶ │  Claude Code CLI │
│  (React/Vite) │ ◀─────────── │   backend/app.py   │ ◀───────────── │  (the "engine")  │
└───────────────┘  text/event  └────────────────────┘  stream-json   └──────────────────┘
     ▲  markdown       -stream        │      ▲                               │  stdio (MCP)
     │  tables                        │      │                               ▼
     │                                │      │                     ┌──────────────────┐
     └── user question ───────────────┘      │                     │   server.py      │
                                             │                     │  (MCP server +   │
                                    streamed │                     │  read-only gate) │
                                    turn      events                └──────────────────┘
                                                                             │ snowflake-
                                                                             ▼ connector
                                                                    ┌──────────────────┐
                                                                    │    Snowflake     │
                                                                    │  (AIRBNB / …)    │
                                                                    └──────────────────┘
```

**The path of a single question:**

1. The React UI `POST`s `{prompt, session}` to `/api/chat` and opens an SSE stream.
2. `backend/app.py` hands the prompt to the **Claude Agent SDK**, which drives the local
   **Claude Code CLI** as the model engine (using your machine's existing login).
3. Claude decides which Snowflake tool to call. The SDK launches **`server.py`** as a
   local **MCP** subprocess over stdio and forwards the tool call.
4. `server.py` runs the SQL through its **permission gate** (read-only by default),
   executes it via `snowflake-connector-python`, and returns rows as JSON.
5. The SDK streams every step back to the backend — thinking, `tool_use`, `tool_result`,
   and the final answer text — which the backend re-emits as **SSE events**.
6. The UI renders the stream live: a "thinking" line, tool chips, then the markdown table.

Everything runs **locally**. Your Snowflake credentials never leave `server.py`, and the
question/answer traffic goes only to the Claude engine you already use for Claude Code.

---

## 2. Components

| Component | File(s) | Responsibility |
|-----------|---------|----------------|
| **MCP server** | `server.py` | Snowflake connection, statement classifier + permission gate, 7 read-only tools. Pure `mcp` SDK + `snowflake-connector-python` + `sqlglot`. |
| **Backend / orchestrator** | `backend/app.py` | FastAPI. Owns the Claude Agent SDK sessions, configures the MCP server inline, streams turns over SSE, serves the built frontend in prod. |
| **Frontend** | `frontend/` | Vite + React + TypeScript + Tailwind v4 + shadcn/ui. Health gate, SSE chat, markdown tables, tool chips, dark theme. |
| **CLI tester** | `query.py` | Run a query straight against `server.py`'s logic without the full stack. |
| **Agents** | `.claude/agents/*.md` | Data-engineer + frontend-engineer subagent personas used during development. |
| **Portable Node** | `.tools/node` | Self-contained Node runtime for the frontend (no global install; moves with the repo). |

### The 7 MCP tools (`mcp__snowflake__*`)

- `get_connection_info` — account / user / role / warehouse / db / schema / version / mode
- `execute_query(sql)` — run one gated SQL statement, rows as JSON
- `list_databases` · `list_schemas(database)` · `list_tables(schema, database)`
- `describe_table(table)` — columns & types
- `preview_table(table, limit)` — sample rows (capped at 100)

### SSE event contract (backend ↔ frontend)

- `GET /api/health` → `{ ready, checks:[{name, ok, detail}], message }` (preflight gate)
- `POST /api/chat {prompt, session}` → SSE `data: {json}\n\n`, where `type` ∈
  `text | thinking | tool_use | tool_result | error | done`
- `POST /api/reset {session}` → clears one conversation

---

## 3. Key decisions & why

### A. Self-owned MCP server, no Cortex
**Decision:** Build our own MCP server on the open protocol + the official Snowflake
connector, rather than use Cortex or a vendor MCP.
**Why:** We control the exact tool surface and the safety gate, and we depend only on
stable, documented interfaces. This is what makes the roadmap (DDL → RCA → self-healing)
safe to grow into without re-platforming. Nothing built for A1 gets thrown away later.

### B. Claude Agent SDK reusing the Claude Code login (not the raw API)
**Decision:** The backend drives the local Claude Code CLI via the Agent SDK instead of
calling the Claude API with an API key.
**Why:** Zero extra credentials and **no separate bill** — it runs on the login you
already have. It also gives us the full agentic loop (multi-step tool use, thinking,
streaming) for free, which is exactly the behavior a data-exploration chat needs.

### C. Read-only permission gate, classified by first keyword
**Decision:** `server.py` classifies each statement by its leading keyword and, in the
default `read_only` mode, permits only `SELECT / SHOW / DESCRIBE / EXPLAIN / USE /
VALUES / LIST / GET`. It also rejects multi-statement calls and classifies `WITH` CTEs
by their terminal statement.
**Why:** Safety by construction — the model literally cannot mutate data or schema in the
default mode; a blocked write returns an explanatory error instead of executing.
**Consequence (this is why a JOIN runs):** the gate cares about what a statement *does*,
not how complex it is. A JOIN, subquery, CTE, window function, or aggregation is still a
`SELECT` — a **read** — so it's allowed. "Read-only" means *no changes to data or schema*,
**not** "simple queries only." Only statements whose first keyword writes (`INSERT`,
`UPDATE`, `DELETE`, `MERGE`, `COPY`, …) or changes schema (`CREATE`, `ALTER`, `DROP`, …)
are rejected.

### D. `ENABLE_TOOL_SEARCH=0` — offer the Snowflake tools directly
**Decision:** The backend passes `env={"ENABLE_TOOL_SEARCH": "0"}` to the Agent SDK.
**Why:** With tool search on, the CLI **defers** MCP tools behind a server-side
`ToolSearch` tool — the model must discover a tool by exact name before it can call it.
For a natural question ("what tables are in AIRBNB?") the model searched by *keyword*,
found nothing, and wrongly reported the Snowflake tools as unavailable. Turning tool
search off offers all 7 `mcp__snowflake__*` tools to the model directly, so it just calls
them. (Note: setting `tools=[]` is **not** a substitute — it also removes `ToolSearch`
itself, leaving no path to the still-deferred MCP tools.)

### E. Inline MCP config + strict/self-contained SDK options
**Decision:** `make_options()` passes the MCP server config inline and sets
`strict_mcp_config=True`, `setting_sources=[]`, and `permission_mode="bypassPermissions"`.
**Why:** The app is self-contained and reproducible — it uses **only** our Snowflake
server and ignores any other MCP config or project/user settings on the machine, so it
behaves the same everywhere. `bypassPermissions` is safe here precisely *because* the
read-only gate in `server.py` is the real backstop.

### F. Single-server production mode
**Decision:** In prod, `app.py` mounts the built `frontend/dist` at `/`, so one process
serves both the UI and the API on port **8000**. Dev mode instead runs Vite on 5173 with
a `/api → 8000` proxy for hot reload.
**Why:** One command, one port, nothing to orchestrate for normal use; the split dev
setup exists only when you're actively editing the frontend.

### G. Per-session Agent SDK client, streamed over SSE
**Decision:** Each `session` id gets its own long-lived `ClaudeSDKClient` (guarded by an
async lock); turns stream to the browser as Server-Sent Events.
**Why:** Conversation memory per session, and the user sees reasoning/tool calls/results
as they happen instead of waiting for one big response.

### H. Portable Node, venv-based Python
**Decision:** Node lives in `.tools/node` (no global install); Python runs from the repo
`.venv`.
**Why:** The toolchain is self-contained and moves with the repo — no machine-wide setup,
and the exact runtimes are pinned.

---

## 4. Running it

**Prod (one server):**
```powershell
cd <repo>\snowflake-mcp-claude
.venv\Scripts\python.exe backend\app.py     # serves UI + API on http://127.0.0.1:8000
```

**Dev (hot reload) — two terminals:**
```powershell
# terminal 1 — backend on 8000
.venv\Scripts\python.exe backend\app.py

# terminal 2 — Vite on 5173 (proxies /api -> 8000)
cd frontend
$env:PATH = "<repo>\snowflake-mcp-claude\.tools\node;$env:PATH"   # put portable Node on PATH
npm run dev            # open http://localhost:5173
```

**Rebuild the frontend after edits** (prod picks it up on reload):
```powershell
cd frontend
$env:PATH = "<repo>\snowflake-mcp-claude\.tools\node;$env:PATH"
npm run build
```

The backend's `/api/health` is a preflight: it must show **all green** (server file,
`.env`, Claude engine + login, MCP connection) before chatting.

---

## 5. Configuration (`.env`, gitignored)

| Var | Purpose |
|-----|---------|
| `SNOWFLAKE_ACCOUNT` / `_USER` / `_PASSWORD` | Connection credentials |
| `SNOWFLAKE_ROLE` / `_WAREHOUSE` / `_DATABASE` / `_SCHEMA` | Session context (all optional) |
| `SNOWFLAKE_MCP_MODE` | Permission mode — `read_only` (default) / `read_write` / `full` |
| `SNOWFLAKE_MCP_MAX_ROWS` | Row cap per query (default 1000) |

**Permission modes:**

| Mode | Allows |
|------|--------|
| `read_only` (default) | SELECT / SHOW / DESCRIBE / EXPLAIN / USE / VALUES / LIST / GET |
| `read_write` | the above + INSERT / UPDATE / DELETE / MERGE / COPY / PUT |
| `full` | the above + DDL (CREATE/ALTER/DROP/…) + admin (GRANT/REVOKE/CALL/…) |

Change the mode in `.env`, then restart the backend.

---

## 6. Roadmap (additive — nothing gets thrown away)

- **A1 (current):** read-only SQL + discovery tools.
- **A2:** DDL/DML via `read_write` / `full` mode + confirmation on destructive ops.
- **A3:** RCA / observability tools (QUERY_HISTORY, TASK_HISTORY, COPY_HISTORY, profiling).
- **A4:** self-healing actions with dry-run, approval gates, audit log, and a scoped role.

---

## 7. Operational notes & gotchas

- **Run the backend from this folder with the `.venv` Python.** `app.py` resolves all
  paths (`server.py`, `.env`, `frontend/dist`) relative to itself. Launching a copy from
  the wrong directory or with system Python is the usual cause of a red `/api/health`.
- **Port 8000** must match the Vite dev proxy target. If you change the backend port,
  update `frontend/vite.config.ts` too.
- **The `.venv` is not relocatable.** After moving the repo, recreate it:
  `py -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt`.
- **Scoped role for the future.** It currently connects as ACCOUNTADMIN; move to a scoped
  role before enabling write/self-healing modes (A4).
- **Primary target data:** database `AIRBNB`, schema `DBT_SCHEMA`, warehouse `COMPUTE_WH`.
