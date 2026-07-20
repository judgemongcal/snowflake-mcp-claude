# Snowflake MCP Server (self-owned, Cortex-free)

A small, future-proof MCP server that lets Claude Code query Snowflake and (later)
run DWH processes. It depends only on the open **MCP protocol** (`mcp` Python SDK)
and Snowflake's officially-supported **`snowflake-connector-python`** — **no Cortex**.
The tool surface and permission gating are fully under our control.

## Layout

| File | Purpose |
|------|---------|
| `server.py` | The MCP server: connection + statement classifier + tools |
| `requirements.txt` | `mcp`, `snowflake-connector-python`, `sqlglot`, `python-dotenv` |
| `.env` | Connection config + permission mode (gitignored — real secrets) |
| `.env.example` | Template for `.env` |
| `.venv/` | Isolated Python env (created with `py -m venv`) |
| `.claude/agents/snowflake-data-engineer.md` | Senior Snowflake data-engineer subagent |

## Setup (already done)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# then register with Claude Code:
claude mcp add snowflake --scope local -- <abs path>\.venv\Scripts\python.exe <abs path>\server.py
```

After registering, **restart Claude Code** so the `mcp__snowflake__*` tools load.

## Permission modes (`SNOWFLAKE_MCP_MODE` in `.env`)

| Mode | Allows |
|------|--------|
| `read_only` (default) | SELECT / SHOW / DESCRIBE / EXPLAIN / USE / VALUES / LIST / GET |
| `read_write` | the above + INSERT / UPDATE / DELETE / MERGE / COPY / PUT |
| `full` | the above + DDL (CREATE/ALTER/DROP/…) + admin (GRANT/REVOKE/CALL/…) |

The gate rejects multi-statement calls and classifies `WITH` CTEs by their terminal
statement (SELECT = read, INSERT/UPDATE/DELETE/MERGE = write). Change the mode in
`.env`, then restart Claude Code.

## Tools (A1 — read-only)

- `get_connection_info` — account / user / role / warehouse / db / schema / version / mode
- `execute_query(sql)` — run one gated SQL statement, rows as JSON
- `list_databases` / `list_schemas(database)` / `list_tables(schema, database)`
- `describe_table(table)` — columns & types
- `preview_table(table, limit)` — sample rows (≤100)

## Roadmap (additive)

- **A1 (current):** read-only SQL + discovery.
- **A2:** DDL/DML via `read_write`/`full` mode + confirmation on destructive ops.
- **A3:** RCA / observability tools (QUERY_HISTORY, TASK_HISTORY, COPY_HISTORY, profiling).
- **A4:** self-healing actions with dry-run, approval gates, audit log, scoped role.

## Notes

- Currently connects as **ACCOUNTADMIN**. Move to a scoped role before real
  self-healing (A4).
- Target DWH: database `AIRBNB`, schema `dbt_schema`, warehouse `COMPUTE_WH`.
