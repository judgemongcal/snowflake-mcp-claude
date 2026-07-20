"""
Snowflake MCP Server (self-owned, Cortex-free).

Future-proof by design: depends only on the open MCP protocol (via the official
`mcp` Python SDK) and Snowflake's officially-supported `snowflake-connector-python`.
No Cortex is ever imported. The tool surface and permission gating are fully under
our control, which is what makes the roadmap below safe to grow into.

Roadmap (additive — nothing here gets thrown away):
  A1 (current): read-only SQL + discovery tools.
  A2: DDL/DML behind a per-category allowlist + confirmation on destructive ops.
  A3: RCA / observability tools (QUERY_HISTORY, TASK_HISTORY, COPY_HISTORY, profiling).
  A4: self-healing actions with dry-run, approval gates, audit log, scoped role.

Permission mode is controlled by the SNOWFLAKE_MCP_MODE env var (default: read_only):
  read_only  -> SELECT / SHOW / DESCRIBE / EXPLAIN / USE / VALUES / LIST / GET
  read_write -> the above + INSERT / UPDATE / DELETE / MERGE / COPY / PUT / ...
  full       -> the above + DDL (CREATE/ALTER/DROP/...) + admin (GRANT/REVOKE/CALL/...)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Load connection config from a local .env next to this file (never committed).
load_dotenv(Path(__file__).parent / ".env")

import snowflake.connector
from snowflake.connector import DictCursor
from mcp.server.fastmcp import FastMCP

try:
    import sqlglot
    from sqlglot import expressions as sqlglot_exp
except Exception:  # sqlglot strongly recommended, but keep server usable without it
    sqlglot = None
    sqlglot_exp = None

MAX_ROWS = int(os.getenv("SNOWFLAKE_MCP_MAX_ROWS", "1000"))
MODE = os.getenv("SNOWFLAKE_MCP_MODE", "read_only").strip().lower()

mcp = FastMCP("snowflake")

# --- statement classification / permission gate --------------------------------

READ_KEYWORDS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "USE", "VALUES", "LIST", "GET"}
WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "COPY", "PUT", "REMOVE", "RM"}
DDL_KEYWORDS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "SWAP", "UNDROP", "COMMENT"}
ADMIN_KEYWORDS = {"GRANT", "REVOKE", "CALL", "EXECUTE", "SET", "UNSET", "BEGIN", "COMMIT", "ROLLBACK"}

MODE_ALLOWED = {
    "read_only": {"read"},
    "read_write": {"read", "write"},
    "full": {"read", "write", "ddl", "admin"},
}


def _strip_sql_comments(sql: str) -> str:
    """Remove -- and /* */ comments (string-aware enough for gating)."""
    out = []
    i, n = 0, len(sql)
    in_str = None
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_str:
            out.append(c)
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _classify_with(cleaned: str) -> str:
    """A WITH (CTE) may terminate in SELECT (read) or INSERT/UPDATE/DELETE/MERGE (write)."""
    if sqlglot is not None:
        try:
            tree = sqlglot.parse_one(cleaned, read="snowflake")
            if isinstance(tree, sqlglot_exp.Select):
                return "read"
            if isinstance(tree, (sqlglot_exp.Insert, sqlglot_exp.Update,
                                 sqlglot_exp.Delete, sqlglot_exp.Merge)):
                return "write"
        except Exception:
            pass
    up = cleaned.upper()
    for kw in WRITE_KEYWORDS:
        if _has_word(up, kw):
            return "write"
    for kw in DDL_KEYWORDS:
        if _has_word(up, kw):
            return "ddl"
    return "read"


def classify(sql: str) -> tuple[str, str]:
    """Return (category, first_keyword). category in {read, write, ddl, admin, unknown}."""
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        return ("unknown", "")
    first = cleaned.split(None, 1)[0].upper()
    if first in READ_KEYWORDS:
        return ("read", first)
    if first in WRITE_KEYWORDS:
        return ("write", first)
    if first in DDL_KEYWORDS:
        return ("ddl", first)
    if first in ADMIN_KEYWORDS:
        return ("admin", first)
    if first == "WITH":
        return (_classify_with(cleaned), first)
    return ("unknown", first)


def _is_multi_statement(sql: str) -> bool:
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    in_str = None
    for c in cleaned:
        if in_str:
            if c == in_str:
                in_str = None
            continue
        if c in ("'", '"'):
            in_str = c
            continue
        if c == ";":
            return True
    return False


def check_permission(sql: str) -> str | None:
    """Return an error string if the statement is blocked, else None."""
    if _is_multi_statement(sql):
        return "Rejected: multiple statements per call are not allowed. Send one statement at a time."
    cat, first = classify(sql)
    allowed = MODE_ALLOWED.get(MODE, {"read"})
    if cat == "unknown":
        return (f"Blocked: could not classify statement (first keyword '{first}'). "
                f"In '{MODE}' mode only read statements are permitted.")
    if cat in allowed:
        return None
    return (f"Blocked by permission mode '{MODE}': this is a {cat.upper()} statement "
            f"(first keyword '{first}'). Ask the user to widen SNOWFLAKE_MCP_MODE to enable it.")


# --- connection ----------------------------------------------------------------

_conn = None


def get_conn():
    global _conn
    if _conn is None or _conn.is_closed():
        _conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ["SNOWFLAKE_PASSWORD"],
            role=os.getenv("SNOWFLAKE_ROLE") or None,
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE") or None,
            database=os.getenv("SNOWFLAKE_DATABASE") or None,
            schema=os.getenv("SNOWFLAKE_SCHEMA") or None,
            client_session_keep_alive=True,
            application="claude-code-snowflake-mcp",
        )
    return _conn


def _run(sql: str) -> dict:
    cur = get_conn().cursor(DictCursor)
    try:
        cur.execute(sql)
        data = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(data) > MAX_ROWS
        rows = data[:MAX_ROWS]
        return {
            "columns": [c[0] for c in (cur.description or [])],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "query_id": cur.sfqid,
        }
    finally:
        cur.close()


def _ident_ok(name: str) -> bool:
    """Allow qualified identifiers (db.schema.obj), quoted or not; block statement smuggling."""
    return bool(name) and ";" not in name and "\n" not in name


# --- tools (A1: read-only) -----------------------------------------------------

@mcp.tool()
def get_connection_info() -> str:
    """Show the current Snowflake session context: account, user, role, warehouse,
    database, schema, Snowflake version, and the MCP permission mode."""
    try:
        res = _run(
            "SELECT CURRENT_ACCOUNT() AS account, CURRENT_USER() AS user, "
            "CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS warehouse, "
            "CURRENT_DATABASE() AS database, CURRENT_SCHEMA() AS schema, "
            "CURRENT_VERSION() AS version"
        )
        info = res["rows"][0] if res["rows"] else {}
        info["permission_mode"] = MODE
        return json.dumps(info, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def execute_query(sql: str) -> str:
    """Run a single SQL statement against Snowflake and return rows as JSON.
    Gated by the server's permission mode: in read_only mode only
    SELECT/SHOW/DESCRIBE/EXPLAIN/USE statements are permitted. Send one statement
    per call (no semicolons chaining multiple statements)."""
    err = check_permission(sql)
    if err:
        return json.dumps({"error": err, "mode": MODE})
    try:
        return json.dumps(_run(sql), default=str)
    except Exception as e:
        return json.dumps({"error": str(e), "sql": sql})


@mcp.tool()
def list_databases() -> str:
    """List all databases visible to the current role."""
    try:
        return json.dumps(_run("SHOW DATABASES"), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_schemas(database: str = "") -> str:
    """List schemas, optionally within a specific database."""
    if database and not _ident_ok(database):
        return json.dumps({"error": "invalid database name"})
    sql = f"SHOW SCHEMAS IN DATABASE {database}" if database else "SHOW SCHEMAS"
    try:
        return json.dumps(_run(sql), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_tables(schema: str = "", database: str = "") -> str:
    """List tables. Provide schema (and optionally database) to scope it, e.g.
    schema='DBT_SCHEMA', database='AIRBNB'. With no args, lists tables in the
    current schema."""
    for v in (schema, database):
        if v and not _ident_ok(v):
            return json.dumps({"error": "invalid identifier"})
    if database and schema:
        sql = f"SHOW TABLES IN SCHEMA {database}.{schema}"
    elif schema:
        sql = f"SHOW TABLES IN SCHEMA {schema}"
    else:
        sql = "SHOW TABLES"
    try:
        return json.dumps(_run(sql), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def describe_table(table: str) -> str:
    """Describe a table's columns and types. Accepts a qualified name, e.g.
    'AIRBNB.DBT_SCHEMA.DIM_LISTINGS'."""
    if not _ident_ok(table):
        return json.dumps({"error": "invalid table name"})
    try:
        return json.dumps(_run(f"DESCRIBE TABLE {table}"), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def preview_table(table: str, limit: int = 20) -> str:
    """Return a small sample of rows from a table (default 20, capped at 100)."""
    if not _ident_ok(table):
        return json.dumps({"error": "invalid table name"})
    limit = max(1, min(int(limit), 100))
    try:
        return json.dumps(_run(f"SELECT * FROM {table} LIMIT {limit}"), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
