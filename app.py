import os
import sqlite3
import random
import json
import streamlit as st
from dotenv import load_dotenv
from groq import Groq

# Load environment variables from .env file (if present)
load_dotenv()

# ------------------- Database Setup -------------------
DB_NAME = "production_mock.db"

def setup_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Orders table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        status TEXT,
        amount REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Telemetry table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS query_performance_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_query TEXT,
        execution_time_ms REAL,
        call_count INTEGER
    );
    """)
    # Populate mock data if empty
    cursor.execute("SELECT count(*) FROM orders")
    if cursor.fetchone()[0] == 0:
        statuses = ['PENDING', 'COMPLETED', 'FAILED', 'SHIPPED']
        dummy_rows = [
            (random.randint(100, 5000), random.choice(statuses), round(random.uniform(50.0, 5000.0), 2))
            for _ in range(5000)
        ]
        cursor.executemany("INSERT INTO orders (customer_id, status, amount) VALUES (?, ?, ?)", dummy_rows)

    # Reset telemetry logs
    cursor.execute("DELETE FROM query_performance_logs")
    cursor.execute("""
    INSERT INTO query_performance_logs (logged_query, execution_time_ms, call_count)
    VALUES 
    ('SELECT * FROM orders WHERE status = "PENDING" AND amount > 500', 1420.5, 840),
    ('SELECT customer_id, count(*) FROM orders GROUP BY customer_id', 980.2, 120),
    ('SELECT * FROM orders WHERE order_id = 10', 0.8, 4500);
    """)
    conn.commit()
    conn.close()

setup_database()

# ------------------- MCP Server -------------------
class SQLiteMCPServer:
    """Expose DBA tooling via MCP-style functions."""
    def __init__(self, db_path=DB_NAME):
        self.db_path = db_path

    def get_tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_slow_queries",
                    "description": "Fetch slow queries exceeding a latency threshold (ms).",
                    "parameters": {
                        "type": "object",
                        "properties": {"threshold_ms": {"type": "number", "description": "Latency threshold (ms)"}},
                        "required": ["threshold_ms"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "inspect_table_schema",
                    "description": "Inspect columns, types and indexes of a table.",
                    "parameters": {
                        "type": "object",
                        "properties": {"table_name": {"type": "string", "description": "Table name"}},
                        "required": ["table_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "explain_query_plan",
                    "description": "Run EXPLAIN QUERY PLAN for a query.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "SQL query"}},
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "dry_run_migration",
                    "description": "Test DDL inside a transaction and roll back. Do NOT include manual BEGIN or COMMIT/ROLLBACK statements.",
                    "parameters": {
                        "type": "object",
                        "properties": {"migration_sql": {"type": "string", "description": "DDL statements to test (e.g., CREATE INDEX ...)"}},
                        "required": ["migration_sql"]
                    }
                }
            }
        ]

    def execute_tool(self, tool_name: str, args: dict) -> dict:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        cur = conn.cursor()
        try:
            if tool_name == "get_slow_queries":
                th = args.get("threshold_ms", 500.0)
                cur.execute(
                    "SELECT logged_query, execution_time_ms, call_count FROM query_performance_logs WHERE execution_time_ms >= ? ORDER BY execution_time_ms DESC",
                    (th,)
                )
                rows = cur.fetchall()
                return {"slow_queries": [{"query": r[0], "latency_ms": r[1], "frequency": r[2]} for r in rows]}

            if tool_name == "inspect_table_schema":
                tbl = args["table_name"]
                cur.execute(f"PRAGMA table_info({tbl});")
                cols = [{"cid": c[0], "name": c[1], "type": c[2]} for c in cur.fetchall()]
                cur.execute(f"PRAGMA index_list({tbl});")
                idx = cur.fetchall()
                return {"table": tbl, "columns": cols, "existing_indexes": idx}

            if tool_name == "explain_query_plan":
                sql = args["query"]
                cur.execute(f"EXPLAIN QUERY PLAN {sql}")
                plan = cur.fetchall()
                return {"plan_nodes": plan}

            if tool_name == "dry_run_migration":
                stmt = args["migration_sql"]
                cur.execute("BEGIN TRANSACTION;")
                try:
                    statements = [s.strip() for s in stmt.split(";") if s.strip()]
                    for s in statements:
                        if s.upper() in ("BEGIN", "BEGIN TRANSACTION", "COMMIT", "ROLLBACK"):
                            continue
                        cur.execute(s)
                    cur.execute("ROLLBACK;")
                    return {"status": "SUCCESS", "message": "Dry-run succeeded; changes safely rolled back."}
                except Exception as run_err:
                    cur.execute("ROLLBACK;")
                    return {"status": "FAILED", "error": str(run_err)}

            return {"error": f"Unknown tool {tool_name}"}
        except Exception as e:
            return {"status": "FAILED", "error": str(e)}
        finally:
            conn.close()

mcp_server = SQLiteMCPServer()

# ------------------- Agent Logic (Groq: openai/gpt-oss-20b) -------------------
def get_groq_api_key() -> str:
    """Retrieve key from .env / OS env first, then Streamlit Secrets."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        try:
            if "GROQ_API_KEY" in st.secrets:
                api_key = st.secrets["GROQ_API_KEY"]
        except Exception:
            pass
    return api_key

def run_dba_agent(user_prompt: str, log_callback):
    api_key = get_groq_api_key()
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY is missing. Add it to your `.env` file (GROQ_API_KEY=gsk_...) "
            "or configure it in Streamlit Cloud Secrets."
        )

    client = Groq(api_key=api_key)

    system_prompt = (
        "You are an Autonomous Database Reliability Agent using Model Context Protocol (MCP).\n"
        "Workflow:\n"
        "1. Identify slow queries from telemetry.\n"
        "2. Inspect schema & execution plan for bottlenecks.\n"
        "3. Propose safe index migrations.\n"
        "4. ALWAYS dry-run the migration via dry_run_migration (do not include BEGIN/COMMIT in SQL).\n"
        "5. Return root-cause analysis, verified migration SQL, and a rollback statement."
    )

    tools = mcp_server.get_tool_definitions()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    max_steps = 10
    step = 0

    while step < max_steps:
        step += 1

        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=2048,
        )

        response_message = response.choices[0].message
        messages.append(response_message)

        # Check if model requested tool executions
        if not response_message.tool_calls:
            log_callback("🟢 Agent completed analysis.")
            return response_message.content or "⚠️ No report generated by the agent."

        # Process each tool call sequentially
        for tool_call in response_message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments or "{}")

            log_callback(f"🔹 **Calling `{name}`** with args: `{json.dumps(args)}`")
            result_dict = mcp_server.execute_tool(name, args)
            log_callback(f"🔸 **Result**: `{json.dumps(result_dict)[:200]}...`")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": name,
                "content": json.dumps(result_dict)
            })

    return "⚠️ Max execution steps reached without a final report."

# ------------------- Streamlit UI -------------------
st.set_page_config(page_title="Autonomous SQLite DBA Agent", layout="centered")
st.title("🛡️ Autonomous SQLite DBA Agent (Groq / MCP)")
st.caption("Model: `openai/gpt-oss-20b` via Groq")

presets = [
    "Identify slow queries over 500ms, examine the orders table, and create safe index migrations.",
    "Check orders table execution plan for amount filters and dry-run an index.",
    "Inspect telemetry logs and provide a complete DBA health audit."
]
selected = st.selectbox("Preset", options=presets, index=0)
goal = st.text_area("Goal", value=selected, height=100)
run_btn = st.button("Run Agent", type="primary")

log_placeholder = st.empty()
report_placeholder = st.empty()

if run_btn:
    logs = []
    def logger(msg: str):
        logs.append(msg)
        log_placeholder.markdown("\n\n".join(logs))

    with st.spinner("Running agent with Groq (openai/gpt-oss-20b)…"):
        try:
            report = run_dba_agent(goal, log_callback=logger)
            report_placeholder.markdown(f"### 📋 DBA Resolution Report\n\n{report}")
        except Exception as e:
            st.error(f"Error: {e}")