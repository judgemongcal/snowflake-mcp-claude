# Snowflake Chat — Session Handoff

_Last updated: 2026-07-20. This file is the source of truth for resuming. When you
start a new Claude Code session in the moved folder, say: "Read HANDOFF.md and
continue." (The old chat's memory is keyed to the old path and won't auto-load here.)_

## What we're building

A standalone **"Claude-like" chat frontend for Snowflake**. You type a question,
Claude (the brain) calls read-only Snowflake tools, and you read the answer back —
with results as markdown tables.

**Architecture (decided):**
- **`server.py`** — a self-owned MCP server (official `mcp` SDK + `snowflake-connector-python` + `sqlglot`). **No Cortex.** Read-only permission gate today; extensible to DDL/RCA/self-healing later. (Option A — chosen for future-proofing.)
- **`backend/app.py`** — FastAPI + **Claude Agent SDK** (Path B). Reuses your Claude Code login (no API key/bill). Talks to `server.py` and streams to the UI over SSE.
- **`frontend/`** — Vite + React + TypeScript + Tailwind v4 + **shadcn/ui** chat UI.
- **Agents:** `.claude/agents/snowflake-data-engineer.md` and `.claude/agents/frontend-engineer.md`.

## Status

### ✅ Done
- `server.py` read-only MCP server — **tested, connects** (account TT20396, Snowflake 10.24.101); read-only gate verified (blocks DROP/DELETE/UPDATE/multi-statement).
- `query.py` — CLI to test queries without restarting (uses relative paths, survives the move).
- `.venv` + Python deps installed (`requirements.txt`).
- `.env` written (gitignored) with Snowflake creds + `SNOWFLAKE_MCP_MODE=read_only`.
- MCP registered in Claude Code via `claude mcp add snowflake` (local scope) — **will need re-registering after the move**, see below.
- Portable **Node v24.18.0** at `.tools/node` (no admin; moves with the folder).
- `frontend/` scaffolded (Vite react-ts); deps installed: tailwind v4, `@tailwindcss/vite`, shadcn deps (cva, clsx, tailwind-merge, `@radix-ui/react-slot`, `@radix-ui/react-scroll-area`), `lucide-react`, `react-markdown`, `remark-gfm`, `tw-animate-css`.
- `frontend/vite.config.ts` — Tailwind plugin + `@`→`src` alias + `/api`→`127.0.0.1:8000` proxy.
- `backend/app.py` — written (endpoints `/api/health`, `/api/chat` SSE, `/api/reset`). **Not yet run/tested.**

### ⏳ Next tasks (in order)
1. **Build the frontend UI** — delegate to the `frontend-engineer` agent. Remaining files:
   - `tsconfig.json` + `tsconfig.app.json`: add `"baseUrl": "."` and `"paths": {"@/*": ["./src/*"]}`.
   - `src/index.css`: Tailwind v4 (`@import "tailwindcss";` + `@import "tw-animate-css";`) + shadcn **neutral** theme CSS variables (light + dark) + `@custom-variant dark`.
   - `components.json` (shadcn, new-york, neutral, `css: src/index.css`, aliases `@/components`, `@/lib/utils`).
   - `src/lib/utils.ts` (`cn`), `src/components/ui/{button,textarea,card}.tsx` (shadcn source).
   - `src/App.tsx`: chat UI — calls `/api/health` (show setup screen if not ready), streams `/api/chat`, renders assistant markdown (tables via remark-gfm), shows tool-call chips, dark default + theme toggle.
   - `index.html`: title "Snowflake Chat" + `class="dark"` on `<html>`.
   - Verify with `npm run build`.
2. **Run & test end-to-end:**
   - Backend: `.venv\Scripts\python.exe backend\app.py` (port 8000).
   - Frontend: `npm run dev` in `frontend/` (port 5173) → open http://localhost:5173.
   - Try: "What tables are in AIRBNB?" → expect streamed answer + table. (Note: only `MY_FIRST_DBT_MODEL` exists so far.)
3. **Nice-to-haves:** `backend/requirements.txt`; a `start.ps1` that launches backend + frontend together; wire the `snowflake-data-engineer` agent persona into the system prompt.

### API contract (backend ↔ frontend)
- `GET /api/health` → `{ ready, checks:[{name,ok,detail}], message }`
- `POST /api/chat {prompt, session}` → SSE `data: {json}\n\n`, `type` ∈ `text | thinking | tool_use | tool_result | error | done`
- `POST /api/reset {session}`

## ⚠️ After you MOVE the folder (e.g. to C:\dev\GitHub\mcp-sf) — do these

Most code uses paths relative to each file, so it mostly survives. These do NOT:

1. **Recreate the venv** (Python venvs are not relocatable):
   ```powershell
   Remove-Item -Recurse -Force .venv
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. **Re-register the MCP server** for use *inside Claude Code* (the standalone app
   doesn't need this — `backend/app.py` passes the MCP config inline):
   ```powershell
   claude mcp remove snowflake
   claude mcp add snowflake --scope local -- <NEWPATH>\.venv\Scripts\python.exe <NEWPATH>\server.py
   ```
3. **Update the hardcoded Node path** in `.claude/agents/frontend-engineer.md`
   (`...\Desktop\mcp-sf\.tools\node` → new location).
4. Node (`.tools/node`) and `server.py`/`query.py`/`backend/app.py` path logic move fine.
5. `frontend/node_modules` moves fine, but if npm errors, run `npm install` again in `frontend/`.

## Security reminder
Rotate the **GitHub PAT** sitting in plaintext in `C:\dev\GitHub\snowflake-dbt-airbnb\.env`
(it's in a comment). And confirm `.env` / `.venv` / `.tools` / `node_modules` are
git-ignored before committing (see root `.gitignore`).
