# Autonomous SQLite DBA Agent (Groq / MCP)

An autonomous Database Reliability Engineer (DBA) agent built with **Streamlit**, powered by **Groq** (`openai/gpt-oss-20b`), and architected around the **Model Context Protocol (MCP)** tool-calling pattern.

The agent monitors database performance telemetry, identifies slow SQL queries, inspects schemas and execution plans, proposes safe index migrations, validates them via dry-run transactions, and produces actionable DBA root-cause audit reports.

---

## Architecture & Workflow

```text
User Request / Preset
         │
         ▼
Streamlit Interface (app.py)
         │
         ▼
Groq LLM (openai/gpt-oss-20b)
   │  ▲
   │  │ Tool Calls / Responses (MCP Protocol)
   ▼  │
SQLiteMCPServer
   ├── 1. get_slow_queries        ──► Inspect telemetry table
   ├── 2. inspect_table_schema    ──► PRAGMA table_info & index_list
   ├── 3. explain_query_plan      ──► EXPLAIN QUERY PLAN analysis
   └── 4. dry_run_migration       ──► DDL test execution + ROLLBACK
         │
         ▼
Final DBA Resolution Report (Root-cause, DDL Migration, Rollback Plan)