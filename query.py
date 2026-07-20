r"""
Quick CLI to test the Snowflake MCP server's logic and SEE output, without
needing to restart Claude Code. Uses the exact same connection + permission
gate as the MCP tools in server.py.

Usage (from the mcp-sf folder, with the venv python):
  .\.venv\Scripts\python.exe query.py                      # show connection info
  .\.venv\Scripts\python.exe query.py "SHOW DATABASES"
  .\.venv\Scripts\python.exe query.py "SELECT * FROM AIRBNB.DBT_SCHEMA.DIM_LISTINGS LIMIT 5"
  .\.venv\Scripts\python.exe query.py "DROP TABLE foo"     # will be BLOCKED (read_only)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server


def print_table(res: dict) -> None:
    cols = res.get("columns", [])
    rows = res.get("rows", [])
    if not cols:
        print("(no columns returned)")
        return
    # rows are dicts keyed by column name
    widths = {c: len(str(c)) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = min(max(widths[c], len(str(r.get(c, "")))), 40)
    line = " | ".join(str(c).ljust(widths[c]) for c in cols)
    print(line)
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r.get(c, ""))[:40].ljust(widths[c]) for c in cols))
    note = f"\n{res.get('row_count', len(rows))} row(s)"
    if res.get("truncated"):
        note += " (truncated)"
    if res.get("query_id"):
        note += f"  [query_id {res['query_id']}]"
    print(note)


def run(sql: str) -> None:
    err = server.check_permission(sql)
    if err:
        print(f"BLOCKED ({server.MODE}): {err}")
        return
    try:
        print_table(server._run(sql))
    except Exception as e:
        print("ERROR:", e)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Permission mode: {server.MODE}\n")
        run(
            "SELECT CURRENT_ACCOUNT() AS account, CURRENT_USER() AS user, "
            "CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS warehouse, "
            "CURRENT_DATABASE() AS database, CURRENT_SCHEMA() AS schema, "
            "CURRENT_VERSION() AS version"
        )
        print("\nTip: pass SQL as an argument, e.g.  python query.py \"SHOW TABLES IN SCHEMA AIRBNB.DBT_SCHEMA\"")
        return
    run(" ".join(sys.argv[1:]))


if __name__ == "__main__":
    main()
