import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from html import escape
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, redirect, render_template, request


APP_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = {**load_env(APP_DIR / ".env"), **os.environ}
OLLAMA_MODEL = ENV.get("OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_URL = ENV.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MCP_IMAGE = ENV.get("ZOHO_ANALYTICS_MCP_IMAGE", "zohoanalytics/mcp-server:latest")
HOST_DATA_DIR = Path(ENV.get("ANALYTICS_MCP_HOST_DATA_DIR", APP_DIR / ".zoho_analytics_mcp_data")).resolve()
CONTAINER_DATA_DIR = ENV.get("ANALYTICS_MCP_DATA_DIR", "/tmp/zoho-analytics-mcp")
LOCAL_DB_PATH = Path(ENV.get("LOCAL_CRM_DB", APP_DIR / "zoho_crm_local.sqlite3")).resolve()
HISTORY_DB_PATH = Path(ENV.get("CHAT_HISTORY_DB", APP_DIR / "chat_history.sqlite3")).resolve()
DATA_SOURCE = ENV.get("DATA_SOURCE", "local").lower()

app = Flask(__name__)







def init_history_db() -> None:
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        con.execute(
            """
            create table if not exists chat_sessions (
                id text primary key,
                title text not null,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists chat_turns (
                id text primary key,
                session_id text,
                created_at text not null,
                data_source text not null,
                user_message text not null,
                answer text not null,
                tool_name text,
                trace_json text not null
            )
            """
        )
        columns = [row[1] for row in con.execute("pragma table_info(chat_turns)")]
        if "session_id" not in columns:
            con.execute("alter table chat_turns add column session_id text")
        if "disliked" not in columns:
            con.execute("alter table chat_turns add column disliked integer not null default 0")
        if "feedback_json" not in columns:
            con.execute("alter table chat_turns add column feedback_json text")
        if "feedback_created_at" not in columns:
            con.execute("alter table chat_turns add column feedback_created_at text")
        con.execute(
            """
            insert or ignore into chat_sessions (id, title, created_at, updated_at)
            values ('default', 'Previous chat', datetime('now'), datetime('now'))
            """
        )
        con.execute("update chat_turns set session_id = 'default' where session_id is null")
        con.commit()
    finally:
        con.close()


def create_chat_session(title: str = "New chat") -> str:
    init_history_db()
    session_id = str(uuid.uuid4())
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        con.execute(
            """
            insert into chat_sessions (id, title, created_at, updated_at)
            values (?, ?, datetime('now'), datetime('now'))
            """,
            (session_id, title),
        )
        con.commit()
    finally:
        con.close()
    return session_id


def ensure_chat_session(session_id: str | None) -> str:
    init_history_db()
    if not session_id:
        return create_chat_session()
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        row = con.execute("select id from chat_sessions where id = ?", (session_id,)).fetchone()
        if row:
            return session_id
    finally:
        con.close()
    return create_chat_session()


def list_chat_sessions() -> list[dict[str, Any]]:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select id, title, created_at, updated_at
            from chat_sessions
            order by updated_at desc
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def update_chat_session(session_id: str, user_message: str) -> None:
    title = user_message.strip().replace("\n", " ")[:60] or "New chat"
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        existing_title = con.execute("select title from chat_sessions where id = ?", (session_id,)).fetchone()
        if existing_title and existing_title[0] == "New chat":
            con.execute("update chat_sessions set title = ? where id = ?", (title, session_id))
        con.execute("update chat_sessions set updated_at = datetime('now') where id = ?", (session_id,))
        con.commit()
    finally:
        con.close()


def save_chat_turn(
    *,
    session_id: str,
    trace_id: str,
    user_message: str,
    answer: str,
    tool_name: str | None,
    trace: dict[str, Any],
) -> None:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        con.execute(
            """
            insert into chat_turns (id, session_id, created_at, data_source, user_message, answer, tool_name, trace_json)
            values (?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                session_id,
                DATA_SOURCE,
                user_message,
                answer,
                tool_name,
                json.dumps(trace, ensure_ascii=False, default=str),
            ),
        )
        title = user_message.strip().replace("\n", " ")[:60] or "New chat"
        existing_title = con.execute("select title from chat_sessions where id = ?", (session_id,)).fetchone()
        if existing_title and existing_title[0] == "New chat":
            con.execute("update chat_sessions set title = ? where id = ?", (title, session_id))
        con.execute("update chat_sessions set updated_at = datetime('now') where id = ?", (session_id,))
        con.commit()
    finally:
        con.close()


def load_chat_history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select id, created_at, data_source, user_message, answer, tool_name, disliked
            from chat_turns
            where session_id = ?
            order by created_at desc
            limit ?
            """,
            (session_id, limit),
        ).fetchall()
        return [
            {
                "trace_id": row["id"],
                "created_at": row["created_at"],
                "data_source": row["data_source"],
                "user_message": row["user_message"],
                "answer": row["answer"],
                "tool_name": row["tool_name"],
                "disliked": bool(row["disliked"]),
            }
            for row in reversed(rows)
        ]
    finally:
        con.close()


def load_trace(trace_id: str) -> dict[str, Any] | None:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        row = con.execute("select trace_json from chat_turns where id = ?", (trace_id,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        con.close()


def build_feedback_diagnostic(user_message: str, answer: str, tool_name: str | None, trace: dict[str, Any]) -> dict[str, Any]:
    try:
        content = ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You review a local CRM assistant response that the user disliked. "
                        "Return only JSON with keys: likely_issue, evidence, better_action, prompt_or_routing_fix, "
                        "data_or_tool_gap, severity. Be concise. Do not apologize. Do not address the user."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": user_message,
                            "answer": answer,
                            "tool_name": tool_name,
                            "trace_summary": {
                                "tool_name": trace.get("tool_name"),
                                "steps": [
                                    {
                                        "name": step.get("name"),
                                        "tool": step.get("tool"),
                                        "reason": step.get("reason"),
                                        "error": step.get("error"),
                                    }
                                    for step in (trace.get("steps") or [])[:8]
                                ],
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            json_mode=True,
        )
        diagnostic = parse_json_object(content)
    except Exception as exc:
        diagnostic = {
            "likely_issue": "Diagnostic generation failed.",
            "evidence": str(exc),
            "better_action": "Review the trace manually.",
            "prompt_or_routing_fix": "",
            "data_or_tool_gap": "",
            "severity": "unknown",
        }
    return diagnostic


def save_dislike_feedback(trace_id: str) -> dict[str, Any] | None:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            select id, user_message, answer, tool_name, trace_json
            from chat_turns
            where id = ?
            """,
            (trace_id,),
        ).fetchone()
        if not row:
            return None
        trace_payload = json.loads(row["trace_json"])
        diagnostic = build_feedback_diagnostic(
            row["user_message"],
            row["answer"],
            row["tool_name"],
            trace_payload,
        )
        con.execute(
            """
            update chat_turns
            set disliked = 1,
                feedback_json = ?,
                feedback_created_at = datetime('now')
            where id = ?
            """,
            (json.dumps(diagnostic, ensure_ascii=False, default=str), trace_id),
        )
        con.commit()
        return diagnostic
    finally:
        con.close()


def load_disliked_feedback(limit: int = 100) -> list[dict[str, Any]]:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select id, created_at, feedback_created_at, user_message, answer, tool_name, feedback_json
            from chat_turns
            where disliked = 1
            order by feedback_created_at desc, created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["feedback"] = json.loads(item.get("feedback_json") or "{}")
            except json.JSONDecodeError:
                item["feedback"] = {"raw": item.get("feedback_json")}
            items.append(item)
        return items
    finally:
        con.close()


def render_trace_value(value: Any) -> str:
    if isinstance(value, dict):
        if "code" in value and isinstance(value["code"], str):
            parts = [f"<h3>Code</h3><pre>{escape(value['code'])}</pre>"]
            remaining = {key: val for key, val in value.items() if key != "code"}
            if remaining:
                parts.append(render_trace_value(remaining))
            return "".join(parts)
        rows = []
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                rendered = render_trace_value(val)
            else:
                rendered = escape(str(val))
            rows.append(f"<tr><th>{escape(str(key))}</th><td>{rendered}</td></tr>")
        return f"<table><tbody>{''.join(rows)}</tbody></table>"

    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            columns = []
            for item in value:
                for key in item.keys():
                    if key not in columns:
                        columns.append(key)
            header = "".join(f"<th>{escape(str(col))}</th>" for col in columns)
            body = ""
            for item in value:
                body += "<tr>" + "".join( f"<td>{escape(str(item.get(col, '')))}</td>" for col in columns) + "</tr>"
            return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"
        return f"<pre>{escape(json.dumps(value, indent=2, ensure_ascii=False, default=str))}</pre>"

    return f"<pre>{escape(str(value))}</pre>"


def analytics_env() -> dict[str, str]:
    env = {
        "ANALYTICS_CLIENT_ID": ENV.get("ANALYTICS_CLIENT_ID") or ENV.get("CLIENT_ID", ""),
        "ANALYTICS_CLIENT_SECRET": ENV.get("ANALYTICS_CLIENT_SECRET") or ENV.get("CLIENT_SECRET", ""),
        "ANALYTICS_REFRESH_TOKEN": ENV.get("ANALYTICS_REFRESH_TOKEN") or ENV.get("REFRESH_TOKEN", ""),
        "ANALYTICS_ORG_ID": ENV.get("ANALYTICS_ORG_ID", ""),
        "ANALYTICS_MCP_DATA_DIR": CONTAINER_DATA_DIR,
        "ACCOUNTS_SERVER_URL": ENV.get("ACCOUNTS_SERVER_URL", "https://accounts.zoho.com"),
        "ANALYTICS_SERVER_URL": ENV.get("ANALYTICS_SERVER_URL", "https://analyticsapi.zoho.com"),
        "QUERY_DATA_RESULT_ROW_LIMITS": ENV.get("QUERY_DATA_RESULT_ROW_LIMITS", "20"),
    }
    required = [
        "ANALYTICS_CLIENT_ID",
        "ANALYTICS_CLIENT_SECRET",
        "ANALYTICS_REFRESH_TOKEN",
        "ANALYTICS_ORG_ID",
        "ANALYTICS_MCP_DATA_DIR",
    ]
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing required .env value(s): {', '.join(missing)}")
    return env


def mcp_server_params():
    from mcp import StdioServerParameters

    HOST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = analytics_env()
    args = ["run", "--rm", "-i"]
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    args.extend(["-v", f"{HOST_DATA_DIR.as_posix()}:{CONTAINER_DATA_DIR}", MCP_IMAGE])
    return StdioServerParameters(command="docker", args=args)


def dump_model(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [dump_model(item) for item in obj]
    if isinstance(obj, dict):
        return {key: dump_model(value) for key, value in obj.items()}
    return obj


async def mcp_list_tools_async() -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": dump_model(tool.inputSchema),
                }
                for tool in result.tools
            ]


async def mcp_call_tool_async(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            return dump_model(result)


def mcp_list_tools() -> list[dict[str, Any]]:
    import anyio

    return anyio.run(mcp_list_tools_async)


def mcp_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    import anyio

    return anyio.run(mcp_call_tool_async, name, arguments)


def ollama_chat_response(
    messages: list[dict[str, Any]],
    *,
    json_mode: bool = False,
    tools: list[dict[str, Any]] | None = None,
    think: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {
            "temperature": 0.1,
            "num_predict": 900,
        },
    }
    if json_mode:
        payload["format"] = "json"
    if tools:
        payload["tools"] = tools
    response = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
    response.raise_for_status()
    return response.json()


def ollama_chat(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    message = ollama_chat_response(messages, json_mode=json_mode)["message"]
    return strip_thinking(message.get("content") or "")


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def extract_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.S | re.I)
    return (match.group(1) if match else text).strip()


def local_schema() -> list[dict[str, Any]]:
    if not LOCAL_DB_PATH.exists():
        raise RuntimeError(f"Local database does not exist: {LOCAL_DB_PATH}")

    con = sqlite3.connect(LOCAL_DB_PATH)
    try:
        tables = [
            row[0]
            for row in con.execute("select name from sqlite_master where type='table' order by name")
            if not row[0].startswith("sqlite_")
        ]
        schema = []
        for table in tables:
            columns = [
                {"name": row[1], "type": row[2] or "TEXT"}
                for row in con.execute(f'pragma table_info("{table}")')
            ]
            count = con.execute(f'select count(*) from "{table}"').fetchone()[0]
            schema.append({"name": table, "columns": columns, "row_count": count})
        return schema
    finally:
        con.close()


def local_tools() -> list[dict[str, Any]]:
    tools = [
        {
            "name": table["name"],
            "description": f"{table['row_count']} rows | "
            + ", ".join(column["name"] for column in table["columns"][:10])
            + ("..." if len(table["columns"]) > 10 else ""),
        }
        for table in local_schema()
    ]
    tools.append(
        {
            "name": "python_analysis",
            "description": "Run a read-only Python analysis script with pandas or Polars against zoho_crm_local.sqlite3.",
        }
    )
    return tools


def local_function_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "local_sql",
                "description": (
                    "Run one read-only SQLite SELECT query against the local CRM database. "
                    "Use this for simple counts, totals, lists, and direct aggregations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": (
                                "A safe SQLite SELECT query using exact table and column names. "
                                "Do not use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, or VACUUM. "
                                "GROUP BY may contain only raw non-aggregate columns. Include LIMIT 50 unless returning aggregate rows."
                            ),
                        }
                    },
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "python_analysis",
                "description": (
                    "Run read-only Python analysis with pandas or Polars against the local CRM SQLite database. "
                    "Use this for multi-table analysis, ranking, recommendations, risk assessment, or complex comparisons."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": (
                                "Short Python code. DB_PATH is already defined. Set JSON-serializable variable result. "
                                "Only use sqlite3, pandas as pd, polars as pl, json, math, statistics, datetime. "
                                "No file writes, network calls, subprocess, os, mutation SQL, or database writes."
                            ),
                        }
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "unsupported_question",
                "description": "Use when the question is not answerable from local CRM business data.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Concrete reason the local CRM data cannot answer this."}
                    },
                    "required": ["reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "clarification_needed",
                "description": "Use when the user asks a CRM question but the required metric, entity, or scope is ambiguous.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The concise clarification question to ask the user."}
                    },
                    "required": ["question"],
                },
            },
        },
    ]


def normalize_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = tool_call.get("function") or {}
    name = function.get("name") or tool_call.get("name") or ""
    arguments = function.get("arguments") or tool_call.get("arguments") or {}
    if isinstance(arguments, str):
        arguments = parse_json_object(arguments) if arguments.strip() else {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def qwen_tool_prompt(tools: list[dict[str, Any]], extra_system: str) -> str:
    rendered_tools = "\n".join(json.dumps(tool, ensure_ascii=False) for tool in tools)
    return (
        "# Tools\n\n"
        "You have access to the following functions:\n\n"
        "<tools>\n"
        f"{rendered_tools}\n"
        "</tools>\n\n"
        "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
        "<tool_call>\n"
        "<function=example_function_name>\n"
        "<parameter=example_parameter_1>\n"
        "value_1\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>\n\n"
        "Function calls MUST follow the specified format: an inner <function=...></function> block "
        "must be nested within <tool_call></tool_call> XML tags. Required parameters MUST be specified. "
        "If there is no function call available, call unsupported_question or clarification_needed.\n\n"
        f"{extra_system}"
    )


def compact_schema_prompt(schema: list[dict[str, Any]]) -> str:
    important_tables = {
        "Leads",
        "Deals",
        "Accounts",
        "Invoices",
        "Employees",
        "Tasks",
        "Calls",
        "Activities",
        "Campaigns",
        "Products",
        "Quotes",
        "Sales_Orders",
    }
    lines = []
    for table in schema:
        if table["name"] not in important_tables:
            continue
        columns = ", ".join(column["name"] for column in table["columns"][:28])
        lines.append(f'{table["name"]}: {columns}')
    return "\n".join(lines)


def parse_qwen_xml_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    match = re.search(
        r"<tool_call>\s*<function=([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*</function>\s*</tool_call>",
        content,
        flags=re.S,
    )
    if not match:
        return None
    name = match.group(1)
    body = match.group(2)
    arguments: dict[str, Any] = {}
    for param, value in re.findall(
        r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*</parameter>",
        body,
        flags=re.S,
    ):
        raw = value.strip()
        if raw.startswith(("{", "[")):
            try:
                arguments[param] = json.loads(raw)
                continue
            except json.JSONDecodeError:
                pass
        arguments[param] = raw
    return name, arguments


def choose_local_native_tool_call(
    user_message: str,
    schema: list[dict[str, Any]],
    previous_error: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    system = (
        "You are a local CRM analytics agent. You must call exactly one provided tool. "
        "Do not answer directly. Never summarize the schema. Use only exact table and column names from the schema. "
        "If the user asks about politics, sports, geography, entertainment, weather, recipes, jokes, or anything outside local CRM data, "
        "call unsupported_question. If the CRM request is ambiguous, call clarification_needed. "
        "For simple counts/totals/lists use local_sql. For multi-step analysis, rankings, recommendations, risk, focus, or comparisons use python_analysis. "
        "For overdue payments use Invoices where Due_Date < date('now'). For sales revenue use Deals.Amount. "
        "For top accounts by revenue, use Accounts plus Deals via Account_Name when useful. "
        "For industry focus, compare Leads or Accounts by Industry using available revenue/count fields. "
        "Your whole response must be a single tool call."
    )
    tools = local_function_tools()
    user_content = (
        f"Question:\n{user_message}\n\n"
        f"Available schema:\n{compact_schema_prompt(schema)}\n\n"
        f"Previous error:\n{previous_error or 'none'}\n\n"
        "Return exactly one function call now."
    )
    response = ollama_chat_response(
        [
            {"role": "system", "content": qwen_tool_prompt(tools, system)},
            {"role": "user", "content": user_content},
        ],
        tools=tools,
        think=OLLAMA_MODEL.lower().startswith("qwen"),
    )
    message = response.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        name, arguments = normalize_tool_call(tool_calls[0])
    else:
        parsed = parse_qwen_xml_tool_call(message.get("content") or "")
        if not parsed:
            raise RuntimeError(f"Ollama did not return a native or Qwen-template tool call. Content: {message.get('content') or ''}")
        name, arguments = parsed
    valid_tools = {tool["function"]["name"] for tool in tools}
    if name not in valid_tools:
        raise RuntimeError(f"Ollama selected an unknown native tool: {name}")
    return name, arguments, response


def schema_prompt(schema: list[dict[str, Any]]) -> str:
    lines = []
    for table in schema:
        columns = ", ".join(f'{column["name"]} {column["type"]}'.strip() for column in table["columns"])
        lines.append(f'{table["name"]}({columns})')
    return "\n".join(lines)


DOMAIN_TERMS = {
    "crm",
    "lead",
    "leads",
    "deal",
    "deals",
    "account",
    "accounts",
    "contact",
    "contacts",
    "task",
    "tasks",
    "call",
    "calls",
    "activity",
    "activities",
    "revenue",
    "amount",
    "stage",
    "status",
    "industry",
    "employee",
    "employees",
    "probability",
    "pipeline",
    "source",
    "campaign",
    "campaigns",
    "product",
    "products",
    "vendor",
    "vendors",
    "quote",
    "quotes",
    "order",
    "orders",
    "invoice",
    "invoices",
    "case",
    "cases",
    "solution",
    "solutions",
    "note",
    "notes",
    "price",
    "stock",
    "inventory",
    "employee",
    "employees",
    "owner",
    "owners",
    "department",
    "departments",
    "role",
    "roles",
    "team",
    "teams",
    "manager",
    "managers",
    "payment",
    "payments",
    "overdue",
    "invoice",
    "invoices",
    "due",
    "balance",
    "grand_total",
    "billing",
}


UNSUPPORTED_TERMS = {
    "profit": "No profit, cost, margin, or expense fields exist in the local CRM schema.",
    "margin": "No profit, cost, margin, or expense fields exist in the local CRM schema.",
    "cost": "No profit, cost, margin, or expense fields exist in the local CRM schema.",
    "expense": "No profit, cost, margin, or expense fields exist in the local CRM schema.",
    "customer satisfaction": "No customer satisfaction or survey fields exist in the local CRM schema.",
    "satisfaction": "No customer satisfaction or survey fields exist in the local CRM schema.",
    "churn": "No churn, subscription, renewal, or cancellation fields exist in the local CRM schema.",
    "subscription": "No subscription fields exist in the local CRM schema.",
    "weather": "Weather data is not present in the local CRM database.",
    "president": "Political or government information is not available in the local CRM schema.",
    "prime minister": "Political or government information is not available in the local CRM schema.",
    "football": "Sports data is not available in the local CRM schema.",
    "soccer": "Sports data is not available in the local CRM schema.",
    "match": "Sports or game data is not available in the local CRM schema.",
    "premier league": "Sports data is not available in the local CRM schema.",
    "movie": "Entertainment data is not available in the local CRM schema.",
    "song": "Entertainment data is not available in the local CRM schema.",
    "poem": "Creative writing is not available in the local CRM schema.",
    "joke": "Entertainment is not available in the local CRM schema.",
    "capital of": "Geographic information is not available in the local CRM schema.",
    "population": "Demographic data is not available in the local CRM schema.",
    "recipe": "Food or recipe data is not available in the local CRM schema.",
    "عاصمة": "Geographic information is not available in the local CRM schema.",
    "رئيس": "Political information is not available in the local CRM schema.",
    "قصيدة": "Creative writing is not available in the local CRM schema.",
    "نكتة": "Entertainment is not available in the local CRM schema.",
}


def clarification_question_reason(user_message: str, schema: list[dict[str, Any]]) -> str | None:
    text = user_message.lower().strip()
    vague_metric_words = ["best", "better", "performance", "performing", "successful", "success"]
    metric_words = [
        "revenue",
        "amount",
        "count",
        "volume",
        "probability",
        "conversion",
        "weighted",
        "average",
        "total",
        "duration",
        "employees",
        "pipeline",
    ]

    if re.fullmatch(r"(which one|which is better|what is better|show performance|compare them)\??", text):
        return "Please specify what entities and metric to compare, for example deals by amount, leads by revenue, or accounts by employees."

    if any(word in text for word in vague_metric_words) and not any(word in text for word in metric_words):
        return "Please specify the success metric, such as revenue, deal amount, probability, lead count, conversion proxy, or activity volume."

    if "campaign" in text and any(word in text for word in vague_metric_words) and not any(word in text for word in metric_words):
        return "Please specify how to judge campaigns, for example by deal count, total amount, average probability, or weighted pipeline."

    return None


def unsupported_question_reason(user_message: str, schema: list[dict[str, Any]]) -> str | None:
    text = user_message.lower()
    # Arabic unsupported terms
    arabic_unsupported = {
        'عاصمة': 'Geographic information is not available in the CRM schema.',
        'رئيس الوزراء': 'Political information is not available in the CRM schema.',
        'رئيس الدولة': 'Political information is not available in the CRM schema.',
        'كرة القدم': 'Sports data is not available in the CRM schema.',
        'مباراة': 'Sports data is not available in the CRM schema.',
        'قصيدة': 'Creative writing is not available in the CRM schema.',
        'نكتة': 'Entertainment is not available in the CRM schema.',
        'اكتب لي': 'Creative writing is not available in the CRM schema.',
        'من هو': 'General knowledge is not available in the CRM schema.',
        'ما هي عاصمة': 'Geographic information is not available in the CRM schema.',
    }
    for term, reason in arabic_unsupported.items():
        if term in text:
            return reason

    # Arabic domain terms - if no arabic business term found, block it
    arabic_business_terms = {
        'عميل', 'عملاء', 'صفقة', 'صفقات', 'حساب', 'فاتورة', 'طلب',
        'منتج', 'مبيعات', 'إيراد', 'مجموع', 'متوسط', 'تحليل', 'حلل',
        'مدفوعات', 'متأخرة', 'فواتير', 'مستحقة', 'دفع', 'سداد','قارن', 'أعلى', 'أقل', 'خط', 'مراحل', 'عروض', 'توقع'
    }
    # Check if Arabic text has no business terms
    arabic_chars = bool(re.search(r'[\u0600-\u06FF]', text))
    if arabic_chars:
        tokens = set(re.findall(r'[\u0600-\u06FF]{2,}', text))
        if tokens and not any(term in text for term in arabic_business_terms):
            return "This question is not related to CRM business data."
        for term, reason in UNSUPPORTED_TERMS.items():
            if term in text:
                return reason

    schema_terms = set(DOMAIN_TERMS)
    for table in schema:
        schema_terms.add(table["name"].lower())
        schema_terms.add(table["name"].lower().rstrip("s"))
        for column in table["columns"]:
            name = column["name"].lower()
            schema_terms.add(name)
            schema_terms.update(part for part in re.split(r"[_\W]+", name) if len(part) > 2)

    tokens = {token for token in re.findall(r"[a-zA-Z_]{3,}", text)}
    meaningful = tokens - {
        "what",
        "which",
        "show",
        "list",
        "give",
        "tell",
        "about",
        "from",
        "with",
        "have",
        "does",
        "were",
        "their",
        "them",
        "most",
        "least",
        "best",
        "worst",
        "average",
        "total",
        "count",
        "analyze",
        "compare",
        "calculate",
    }
    if meaningful and not any(token in schema_terms for token in meaningful):
        return "The question does not appear to reference data available in the local CRM schema."

    return None


def is_read_only_sql(sql: str) -> bool:
    cleaned = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", cleaned, flags=re.I):
        return False
    blocked = r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum)\b"
    return re.search(blocked, cleaned, flags=re.I) is None and not aggregate_in_group_by(cleaned)


def aggregate_in_group_by(sql: str) -> bool:
    match = re.search(r"\bgroup\s+by\b(.*?)(\border\s+by\b|\bhaving\b|\blimit\b|$)", sql, flags=re.I | re.S)
    if not match:
        return False
    group_clause = match.group(1)
    return bool(re.search(r"\b(count|sum|avg|min|max|total)\s*\(", group_clause, flags=re.I))


def choose_local_sql(user_message: str, schema: list[dict[str, Any]], previous_error: str | None = None) -> str:
    text = user_message.lower()
    
    # Deterministic overrides
    if any(w in text for w in ['overdue', 'overdue payment', 'overdue invoice', 'المتأخرة', 'متأخر', 'المتأخر', 'مدفوعات متأخرة', 'فواتير متأخرة']):
        return "SELECT Subject, Account_Name, Grand_Total, Due_Date, Invoice_Status FROM Invoices WHERE Due_Date < date('now') ORDER BY Due_Date ASC LIMIT 50"

    table_names = {table["name"].lower(): table["name"] for table in schema}

    for lower_name, table_name in table_names.items():
        singular = lower_name[:-1] if lower_name.endswith("s") else lower_name
        if ("count" in text or "how many" in text) and (lower_name in text or singular in text):
            return f'select count(*) as count from "{table_name}"'
        if any(word in text for word in ["show", "list", "sample", "examples"]) and (
            lower_name in text or singular in text
        ):
            return f'select * from "{table_name}" limit 10'

    content = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Generate one read-only SQLite SELECT query for the user's CRM question. "
                    "Return only JSON with key sql. Use only exact table and column names from the provided schema. "
                    "Do not invent tables, columns, relationships, or renamed fields. "
                    "GROUP BY may contain only raw non-aggregate columns. Never put COUNT, SUM, AVG, MIN, MAX, "
                    "or calculated aggregate expressions in the GROUP BY clause. Put aggregate expressions in SELECT, "
                    "HAVING, or ORDER BY instead. "
                    "Always include a LIMIT of 50 or less unless the query returns aggregate rows. "
                    "When asked about 'industry', always use the Industry column from the Accounts table, never from Lead_Status or any status field. "
                    "When asked about 'overdue payments' or 'overdue invoices', use the Invoices table with Due_Date < date('now') to find overdue records. The status column is Invoice_Status. "
                    "Only answer questions that are strictly about CRM business data. "
                    "If the question is about sports, politics, entertainment, geography, or any non-business topic, "
                    "do not attempt to answer it from the database. "
                    "When asked about 'industry', always use the Industry column from the Accounts table, never from Lead_Status."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": user_message, "schema": schema_prompt(schema), "previous_error": previous_error},
                    ensure_ascii=False,
                ),
            },
        ],
        json_mode=True,
    )
    sql = parse_json_object(content).get("sql", "")
    if not isinstance(sql, str) or not is_read_only_sql(sql):
        raise RuntimeError(f"Ollama generated unsafe or invalid SQL: {sql}")
    if " limit " not in sql.lower() and not re.search(r"\bcount\s*\(|\bsum\s*\(|\bavg\s*\(|\bmin\s*\(|\bmax\s*\(", sql, re.I):
        sql = f"{sql.rstrip(';')} limit 50"
    return sql


def execute_local_sql(sql: str) -> dict[str, Any]:
    if not is_read_only_sql(sql):
        raise RuntimeError("Only read-only SELECT queries are allowed.")
    con = sqlite3.connect(LOCAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql).fetchmany(50)
        return {"sql": sql, "rows": [dict(row) for row in rows], "row_count": len(rows)}
    finally:
        con.close()


def safe_table_name(table: str) -> str:
    tables = {item["name"] for item in local_schema()}
    if table not in tables:
        raise RuntimeError(f"Unknown table: {table}")
    return table


def local_table_rows(table: str, limit: int, offset: int) -> dict[str, Any]:
    table = safe_table_name(table)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    con = sqlite3.connect(LOCAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in con.execute(f'pragma table_info("{table}")')]
        total = con.execute(f'select count(*) from "{table}"').fetchone()[0]
        rows = con.execute(f'select * from "{table}" limit ? offset ?', (limit, offset)).fetchall()
        return {
            "table": table,
            "columns": columns,
            "rows": [dict(row) for row in rows],
            "total_rows": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        con.close()


def wants_python_tool(user_message: str) -> bool:
    text = user_message.lower()
    return any(
        token in text
        for token in [
            "python",
            "pandas",
            "polars",
            "dataframe",
            "data frame",
            "groupby",
            "group by",
            "correlation",
            "plot",
            "chart",
            "analytics",
            "analysis",
            "analyze",
            "breakdown",
            "distribution",
            "trend",
            "compare",
            "average",
            "sum",
            "total",
            "highest",
            "lowest",
            "top ",
            " by ",
            "risk",
            "risky",
            "operation",
            "operations",
            "workload",
            "quality",
            "funnel",
            "focus",
            "recommend",
            "recommendation",
            "تحليل",
            "حلل",
            "إجمالي",
            "إيرادات",
            "مبيعات",
            "متوسط",
            "مجموع",
            "أعلى",
            "أقل",
            "حسب",
            "قطاع",
            "مرحلة",
            "عدد",
            "كم",
            "قارن",
            "توزيع",
            "اتجاه",
            "أداء",
            "تقرير",
            "ملخص",
            "خط المبيعات",
            "أفضل",
            "أسوأ",
            "نسبة",
            "تحليل",
            "إجمالي",
            "إيرادات",
            "مبيعات",
            "متوسط",
            "مجموع",
            "أعلى",
            "أقل",
            "حسب",
            "قطاع",
            "مرحلة",
            "عدد",
            "كم",
            "حلل", 
            "قارن",
            "أعلى",
            "أقل",
            "متوسط",
            "مجموع",
            "توزيع",
            "اتجاه",
            "أداء",
            "تقرير",
            "ملخص",
            "خط المبيعات",
            "أفضل",
            "أسوأ",
            "نسبة",
        ]
    )


def validate_python_script(code: str) -> None:
    blocked = [
        r"\bopen\s*\(",
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"\bcompile\s*\(",
        r"\b__import__\s*\(",
        r"\bsubprocess\b",
        r"\bos\.",
        r"\bshutil\b",
        r"\bsocket\b",
        r"\brequests\b",
        r"\burllib\b",
        r"\bpathlib\b",
        r"\bwrite\s*\(",
        r"\bto_csv\s*\(",
        r"\bto_excel\s*\(",
        r"\bto_parquet\s*\(",
        r"\bto_database\s*\(",
        r"\bto_sql\s*\(",
        r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum)\b",
    ]
    for pattern in blocked:
        if re.search(pattern, code):
            raise RuntimeError(f"Python script contains a blocked operation: {pattern}")


def generate_python_script(user_message: str, schema: list[dict[str, Any]], previous_error: str | None = None) -> str:
    content = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Generate only Python code, no Markdown and no explanation. "
                    "Generate a short read-only Python script for CRM analysis. "
                    "Use exact table and column names from the provided schema. Never invent renamed columns. "
                    "Only perform read-only analysis. Do not execute INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, or VACUUM. "
                    "Use pandas as pd, polars as pl, sqlite3, json, math, statistics, datetime only. "
                    "For Polars, read rows with sqlite3 or pandas first, then use pl.from_pandas(df). "
                    "The variable DB_PATH is already defined. Do not write files, make network calls, "
                    "use subprocess, use os, or mutate the database. Set a JSON-serializable variable named result. "
                    "Keep the script under 20 lines."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {user_message}\n\n"
                    f"Schema:\n{schema_prompt(schema)}\n\n"
                    f"Previous error to fix: {previous_error or 'none'}\n\n"
                    "Example style:\n"
                    "import sqlite3\n"
                    "import pandas as pd\n"
                    "con = sqlite3.connect(DB_PATH)\n"
                    "df = pd.read_sql_query('select Stage, Amount from Deals', con)\n"
                    "result = df.groupby('Stage', as_index=False)['Amount'].sum().to_dict(orient='records')"
                ),
            },
        ]
    )
    code = extract_code(content)
    if not isinstance(code, str) or not code.strip():
        raise RuntimeError("Ollama did not return Python code.")
    validate_python_script(code)
    return code.strip()


def run_python_analysis(code: str) -> dict[str, Any]:
    runner = f"""
import json
import sqlite3
import math
import statistics
from datetime import date, datetime
import pandas as pd
import polars as pl

DB_PATH = {str(LOCAL_DB_PATH)!r}

{code}

def clean(value):
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    if hasattr(value, "to_pandas"):
        return value.to_pandas().to_dict(orient="records")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {{str(k): clean(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value

print(json.dumps({{"result": clean(result)}}, ensure_ascii=False, default=str))
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(runner)
        script_path = handle.name

    try:
        completed = subprocess.run(
            [sys.executable, script_path],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Python analysis failed.")

    stdout = completed.stdout.strip()
    payload = json.loads(stdout.splitlines()[-1])
    return {"code": code, "result": payload.get("result")}


def deterministic_python_analysis(user_message: str) -> str | None:
    text = user_message.lower()
    wants_polars = "polars" in text

    if "deal" in text and "stage" in text and "amount" in text and "probability" in text and any(
        token in text for token in ["average", "avg", "mean"]
    ):
        if wants_polars:
            return (
                "import sqlite3\n"
                "import pandas as pd\n"
                "import polars as pl\n"
                "con = sqlite3.connect(DB_PATH)\n"
                "df = pd.read_sql_query('select Stage, Amount, Probability from Deals', con)\n"
                "pl_df = pl.from_pandas(df)\n"
                "result = pl_df.group_by('Stage').agg([pl.col('Amount').sum().alias('total_amount'), pl.col('Probability').mean().alias('avg_probability')]).sort('total_amount', descending=True).to_dicts()"
            )
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "df = pd.read_sql_query('select Stage, Amount, Probability from Deals', con)\n"
            "result = (df.groupby('Stage', as_index=False)\n"
            "  .agg(total_amount=('Amount', 'sum'), avg_probability=('Probability', 'mean'))\n"
            "  .sort_values('total_amount', ascending=False)\n"
            "  .to_dict(orient='records'))"
        )

    if any(token in text for token in ["risk", "risky", "sales operation", "focus next week", "workload", "حلل", "مسار المبيعات", "أضعف", "مرحلة"]):
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "deals = pd.read_sql_query('select Stage, Amount, Probability, Sales_Cycle_Duration from Deals', con)\n"
            "leads = pd.read_sql_query('select Lead_Status, Industry, Annual_Revenue, No_of_Employees from Leads', con)\n"
            "tasks = pd.read_sql_query('select Status, Priority, Due_Date from Tasks', con)\n"
            "calls = pd.read_sql_query('select Call_Status, Call_Duration, Call_Type from Calls', con)\n"
            "deals['weighted_pipeline'] = deals['Amount'] * deals['Probability'] / 100\n"
            "result = {\n"
            "  'deal_stage_risk': deals.groupby('Stage', as_index=False).agg(deal_count=('Stage','size'), total_amount=('Amount','sum'), avg_probability=('Probability','mean'), weighted_pipeline=('weighted_pipeline','sum'), avg_cycle_days=('Sales_Cycle_Duration','mean')).sort_values('weighted_pipeline', ascending=False).to_dict(orient='records'),\n"
            "  'lead_quality': leads.groupby(['Lead_Status','Industry'], as_index=False).agg(lead_count=('Lead_Status','size'), avg_annual_revenue=('Annual_Revenue','mean'), avg_employees=('No_of_Employees','mean')).sort_values('avg_annual_revenue', ascending=False).head(10).to_dict(orient='records'),\n"
            "  'task_workload': tasks.groupby(['Status','Priority'], as_index=False).size().rename(columns={'size':'task_count'}).sort_values('task_count', ascending=False).head(10).to_dict(orient='records'),\n"
            "  'call_workload': calls.groupby(['Call_Status','Call_Type'], as_index=False).agg(call_count=('Call_Status','size'), avg_call_duration=('Call_Duration','mean')).sort_values('call_count', ascending=False).head(10).to_dict(orient='records')\n"
            "}"
        )

    if "deal" in text and "stage" in text and any(token in text for token in ["amount", "revenue", "total", "sum"]):
        if wants_polars:
            return (
                "import sqlite3\n"
                "import pandas as pd\n"
                "import polars as pl\n"
                "con = sqlite3.connect(DB_PATH)\n"
                "df = pd.read_sql_query('select Stage, Amount from Deals', con)\n"
                "pl_df = pl.from_pandas(df)\n"
                "result = pl_df.group_by('Stage').agg(pl.col('Amount').sum().alias('total_amount')).sort('total_amount', descending=True).to_dicts()"
            )
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "df = pd.read_sql_query('select Stage, Amount from Deals', con)\n"
            "result = df.groupby('Stage', as_index=False)['Amount'].sum().rename(columns={'Amount': 'total_amount'}).sort_values('total_amount', ascending=False).to_dict(orient='records')"
        )

    if "deal" in text and "stage" in text and any(token in text for token in ["count", "many", "number"]):
        if wants_polars:
            return (
                "import sqlite3\n"
                "import pandas as pd\n"
                "import polars as pl\n"
                "con = sqlite3.connect(DB_PATH)\n"
                "df = pd.read_sql_query('select Stage from Deals', con)\n"
                "pl_df = pl.from_pandas(df)\n"
                "result = pl_df.group_by('Stage').len(name='count').sort('count', descending=True).to_dicts()"
            )
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "df = pd.read_sql_query('select Stage from Deals', con)\n"
            "result = df.groupby('Stage').size().reset_index(name='count').sort_values('count', ascending=False).to_dict(orient='records')"
        )

    if ("lead" in text and "industry" in text or "العملاء المحتملين" in text and "القطاع" in text) and (
        "annual_revenue" in text
        or "annual revenue" in text
        or "employee count" in text
        or "employees" in text
        or "القطاع" in text
        or "نركز" in text
    ):
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "df = pd.read_sql_query('select Industry, Annual_Revenue, No_of_Employees from Leads', con)\n"
            "result = (df.groupby('Industry', as_index=False)\n"
            "  .agg(lead_count=('Industry', 'size'), avg_annual_revenue=('Annual_Revenue', 'mean'), avg_employee_count=('No_of_Employees', 'mean'))\n"
            "  .sort_values('avg_annual_revenue', ascending=False)\n"
            "  .head(5)\n"
            "  .to_dict(orient='records'))"
        )

    if "lead" in text and "industry" in text and any(token in text for token in ["count", "many", "number"]):
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "df = pd.read_sql_query('select Industry from Leads', con)\n"
            "result = df.groupby('Industry').size().reset_index(name='count').sort_values('count', ascending=False).to_dict(orient='records')"
        )

    if "الحسابات" in text and any(token in text for token in ["أفضل", "أداء", "الإيرادات"]):
        return (
            "import sqlite3\n"
            "import pandas as pd\n"
            "con = sqlite3.connect(DB_PATH)\n"
            "deals = pd.read_sql_query('select Account_Name, Amount, Stage, Probability from Deals', con)\n"
            "deals['weighted_revenue'] = deals['Amount'] * deals['Probability'] / 100\n"
            "result = (deals.groupby('Account_Name', as_index=False)\n"
            "  .agg(deal_count=('Account_Name','size'), total_revenue=('Amount','sum'), weighted_revenue=('weighted_revenue','sum'), avg_probability=('Probability','mean'))\n"
            "  .sort_values('total_revenue', ascending=False)\n"
            "  .head(10)\n"
            "  .to_dict(orient='records'))"
        )

    return None


def answer_from_local(user_message: str) -> dict[str, Any]:
    schema = local_schema()
    clarification_reason = clarification_question_reason(user_message, schema)
    if clarification_reason:
        trace = {
            "tool_name": "clarification_needed",
            "steps": [
                {
                    "name": "route",
                    "tool": "clarification_needed",
                    "reason": "Question is underspecified; running analysis would require choosing an unstated metric.",
                    "input": {"question": user_message},
                },
                {
                    "name": "inspect_local_schema",
                    "tool": "local_schema",
                    "input": {"database": str(LOCAL_DB_PATH)},
                    "output": {
                        "tables": [
                            {
                                "name": table["name"],
                                "row_count": table["row_count"],
                                "columns": [column["name"] for column in table["columns"]],
                            }
                            for table in schema
                        ]
                    },
                },
                {"name": "ask_for_clarification", "output": {"reason": clarification_reason}},
            ],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": {"reason": clarification_reason},
            "timings_ms": {"total": 0},
        }
        return {
            "answer": clarification_reason,
            "tool_name": "clarification_needed",
            "tool_result": {"reason": clarification_reason},
            "trace": trace,
        }

    unsupported_reason = unsupported_question_reason(user_message, schema)
    if unsupported_reason:
        trace = {
            "tool_name": "unsupported_question",
            "steps": [
                {
                    "name": "route",
                    "tool": "unsupported_question",
                    "reason": "Question cannot be answered from available local CRM tables/columns.",
                    "input": {"question": user_message},
                },
                {
                    "name": "inspect_local_schema",
                    "tool": "local_schema",
                    "input": {"database": str(LOCAL_DB_PATH)},
                    "output": {
                        "tables": [
                            {
                                "name": table["name"],
                                "row_count": table["row_count"],
                                "columns": [column["name"] for column in table["columns"]],
                            }
                            for table in schema
                        ]
                    },
                },
                {
                    "name": "unsupported_answer",
                    "output": {"reason": unsupported_reason},
                },
            ],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": {"reason": unsupported_reason},
            "timings_ms": {"total": 0},
        }
        answer = f"I can't answer that from the local CRM database. {unsupported_reason}"
        return {"answer": answer, "tool_name": "unsupported_question", "tool_result": {"reason": unsupported_reason}, "trace": trace}

    if wants_python_tool(user_message):
        started = time.perf_counter()
        steps = [
            {
                "name": "route",
                "tool": "python_analysis",
                "reason": "Prompt matched analytical/dataframe keywords.",
                "input": {"question": user_message},
            }
        ]
        schema_step_started = time.perf_counter()
        steps.append(
            {
                "name": "inspect_local_schema",
                "tool": "local_schema",
                "input": {"database": str(LOCAL_DB_PATH)},
                "output": {
                    "tables": [
                        {
                            "name": table["name"],
                            "row_count": table["row_count"],
                            "columns": [column["name"] for column in table["columns"]],
                        }
                        for table in schema
                    ]
                },
                "duration_ms": round((time.perf_counter() - schema_step_started) * 1000, 2),
            }
        )
        code_source = "ollama_generated"
        try:
            code = generate_python_script(user_message, schema)
        except Exception as exc:
            fallback = deterministic_python_analysis(user_message)
            if not fallback:
                raise
            code = fallback
            code_source = "deterministic_fallback"
            steps.append({"name": "code_generation_error", "error": str(exc)})
        generated_at = time.perf_counter()
        steps.append(
            {
                "name": "generate_python_code",
                "source": code_source,
                "output": {"code": code},
                "duration_ms": round((generated_at - started) * 1000, 2),
            }
        )
        try:
            result = run_python_analysis(code)
            repair_error = None
        except Exception as exc:
            repair_error = str(exc)
            steps.append({"name": "python_execution_error", "error": repair_error, "input": {"code": code}})
            fallback = deterministic_python_analysis(user_message)
            if fallback and fallback != code:
                code = fallback
                steps.append({"name": "repair_python_code", "source": "deterministic_fallback", "output": {"code": code}})
            else:
                code = generate_python_script(user_message, schema, previous_error=repair_error)
                steps.append({"name": "repair_python_code", "source": "ollama_generated", "output": {"code": code}})
            result = run_python_analysis(code)
        executed_at = time.perf_counter()
        steps.append(
            {
                "name": "execute_python_analysis",
                "input": {"database": str(LOCAL_DB_PATH), "code": code},
                "output": result,
                "duration_ms": round((executed_at - generated_at) * 1000, 2),
            }
        )
        answer = ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Answer only from the provided Python/pandas/Polars analysis result. "
                        "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
                        "Do not infer, assume, or invent tables, columns, records, trends, or causes not shown in the result. "
                        "Be concise and include numbers/group labels when present. "
                        "Do not mention SQL, Python, pandas, Polars, code, tool names, traces, or implementation details unless the user explicitly asks for them."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": user_message, "analysis": result},
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        finished_at = time.perf_counter()
        steps.append(
            {
                "name": "generate_answer",
                "input": {"question": user_message, "analysis_result": result},
                "output": {"answer": answer},
                "duration_ms": round((finished_at - executed_at) * 1000, 2),
            }
        )
        trace = {
            "tool_name": "python_analysis",
            "steps": steps,
            "tool_input": {
                "question": user_message,
                "database": str(LOCAL_DB_PATH),
                "generated_code": code,
                "repaired_after_error": repair_error,
            },
            "tool_output": result,
            "timings_ms": {
                "code_generation": round((generated_at - started) * 1000, 2),
                "python_execution": round((executed_at - generated_at) * 1000, 2),
                "answer_generation": round((finished_at - executed_at) * 1000, 2),
                "total": round((finished_at - started) * 1000, 2),
            },
        }
        return {"answer": answer, "tool_name": "python_analysis", "tool_result": result, "trace": trace}

    started = time.perf_counter()
    steps = [
        {
            "name": "route",
            "tool": "local_sql",
            "reason": "Prompt did not require Python analysis.",
            "input": {"question": user_message},
        }
    ]
    schema_step_started = time.perf_counter()
    steps.append(
        {
            "name": "inspect_local_schema",
            "tool": "local_schema",
            "input": {"database": str(LOCAL_DB_PATH)},
            "output": {
                "tables": [
                    {
                        "name": table["name"],
                        "row_count": table["row_count"],
                        "columns": [column["name"] for column in table["columns"]],
                    }
                    for table in schema
                ]
            },
            "duration_ms": round((time.perf_counter() - schema_step_started) * 1000, 2),
        }
    )
    sql_error = None
    try:
        sql = choose_local_sql(user_message, schema)
        generated_at = time.perf_counter()
        steps.append(
            {
                "name": "generate_sql",
                "output": {"sql": sql},
                "duration_ms": round((generated_at - started) * 1000, 2),
            }
        )
        result = execute_local_sql(sql)
    except Exception as exc:
        sql_error = str(exc)
        steps.append({"name": "sql_error", "error": sql_error})
        sql = choose_local_sql(user_message, schema, previous_error=sql_error)
        steps.append({"name": "repair_sql", "source": "ollama_generated", "output": {"sql": sql}})
        result = execute_local_sql(sql)
        generated_at = time.perf_counter()

    executed_at = time.perf_counter()
    steps.append(
        {
            "name": "execute_sql",
            "input": {"database": str(LOCAL_DB_PATH), "sql": sql},
            "output": result,
            "duration_ms": round((executed_at - generated_at) * 1000, 2),
        }
    )
    answer = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Answer the user's CRM question from the SQLite query result. "
                    "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
                    "Do not infer, assume, or invent tables, columns, records, trends, or causes not shown in the result. "
                    "Be concise. Do not mention SQL, query text, database internals, tool names, traces, or implementation details unless the user explicitly asks for them."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": user_message, "query_result": result},
                    ensure_ascii=False,
                ),
            },
        ]
    )
    finished_at = time.perf_counter()
    steps.append(
        {
            "name": "generate_answer",
            "input": {"question": user_message, "query_result": result},
            "output": {"answer": answer},
            "duration_ms": round((finished_at - executed_at) * 1000, 2),
        }
    )
    trace = {
        "tool_name": "local_sql",
        "steps": steps,
        "tool_input": {
            "question": user_message,
            "database": str(LOCAL_DB_PATH),
            "sql": sql,
            "repaired_after_error": sql_error,
        },
        "tool_output": result,
        "timings_ms": {
            "sql_generation": round((generated_at - started) * 1000, 2),
            "sql_execution": round((executed_at - generated_at) * 1000, 2),
            "answer_generation": round((finished_at - executed_at) * 1000, 2),
            "total": round((finished_at - started) * 1000, 2),
        },
    }
    return {"answer": answer, "tool_name": "local_sql", "tool_result": result, "trace": trace}


def answer_from_local(user_message: str) -> dict[str, Any]:
    schema = local_schema()
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []

    schema_step_started = time.perf_counter()
    steps.append(
        {
            "name": "inspect_local_schema",
            "tool": "local_schema",
            "input": {"database": str(LOCAL_DB_PATH)},
            "output": {
                "tables": [
                    {
                        "name": table["name"],
                        "row_count": table["row_count"],
                        "columns": [column["name"] for column in table["columns"]],
                    }
                    for table in schema
                ]
            },
            "duration_ms": round((time.perf_counter() - schema_step_started) * 1000, 2),
        }
    )

    tool_started = time.perf_counter()
    try:
        tool_name, tool_args, native_response = choose_local_native_tool_call(user_message, schema)
    except Exception as exc:
        fallback_code = deterministic_python_analysis(user_message)
        if not fallback_code:
            raise
        tool_name = "python_analysis"
        tool_args = {"code": fallback_code}
        native_response = {"fallback_reason": str(exc)}
    deterministic_unsupported = unsupported_question_reason(user_message, schema)
    deterministic_clarification = clarification_question_reason(user_message, schema)
    if deterministic_unsupported and tool_name not in {"unsupported_question", "clarification_needed"}:
        tool_name = "unsupported_question"
        tool_args = {"reason": deterministic_unsupported}
    if deterministic_clarification and tool_name not in {"unsupported_question", "clarification_needed"}:
        tool_name = "clarification_needed"
        tool_args = {"question": deterministic_clarification}
    steps.append(
        {
            "name": "native_function_call",
            "tool": "ollama_native_tool_call",
            "reason": "Ollama returned message.tool_calls instead of prompt-formatted JSON.",
            "input": {"question": user_message, "available_tools": [tool["function"]["name"] for tool in local_function_tools()]},
            "output": {"tool_name": tool_name, "arguments": tool_args},
            "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2),
        }
    )

    if tool_name == "clarification_needed":
        answer = tool_args.get("question") or "Please clarify what metric or scope you want me to use."
        trace = {
            "tool_name": tool_name,
            "steps": steps + [{"name": "ask_for_clarification", "output": {"question": answer}}],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": {"question": answer},
            "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
            "native_function_call": True,
        }
        return {"answer": answer, "tool_name": tool_name, "tool_result": {"question": answer}, "trace": trace}

    if tool_name == "unsupported_question":
        reason = tool_args.get("reason") or "The question cannot be answered from the local CRM schema."
        answer = f"I can't answer that from the local CRM database. {reason}"
        trace = {
            "tool_name": tool_name,
            "steps": steps + [{"name": "unsupported_answer", "output": {"reason": reason}}],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": {"reason": reason},
            "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
            "native_function_call": True,
        }
        return {"answer": answer, "tool_name": tool_name, "tool_result": {"reason": reason}, "trace": trace}

    if tool_name == "python_analysis":
        code = str(tool_args.get("code") or "").strip()
        if not code:
            fallback = deterministic_python_analysis(user_message)
            if not fallback:
                raise RuntimeError("Native tool call selected python_analysis without code.")
            code = fallback
            steps.append({"name": "native_argument_repair", "source": "deterministic_fallback", "output": {"code": code}})
        try:
            validate_python_script(code)
            result = run_python_analysis(code)
        except Exception as exc:
            repair_started = time.perf_counter()
            steps.append({"name": "execute_python_analysis", "input": {"database": str(LOCAL_DB_PATH), "code": code}, "error": str(exc)})
            tool_name_2, tool_args_2, _ = choose_local_native_tool_call(user_message, schema, previous_error=str(exc))
            if tool_name_2 != "python_analysis" or not tool_args_2.get("code"):
                fallback = deterministic_python_analysis(user_message)
                if not fallback:
                    raise
                code = fallback
                repair_source = "deterministic_fallback"
            else:
                code = str(tool_args_2["code"])
                repair_source = "ollama_native_tool_call"
            try:
                validate_python_script(code)
                result = run_python_analysis(code)
            except Exception:
                fallback = deterministic_python_analysis(user_message)
                if not fallback:
                    raise
                code = fallback
                validate_python_script(code)
                result = run_python_analysis(code)
            steps.append(
                {
                    "name": "repair_python_analysis",
                    "tool": repair_source,
                    "input": {"previous_error": str(exc)},
                    "output": {"code": code},
                    "duration_ms": round((time.perf_counter() - repair_started) * 1000, 2),
                }
            )
        steps.append({"name": "execute_python_analysis", "tool": "python_analysis", "input": {"database": str(LOCAL_DB_PATH), "code": code}, "output": result})
        answer = ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Answer the user's CRM question from the Python analysis result. "
                        "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
                        "Do not infer or invent data. Be concise. Do not mention SQL, Python code, tool names, traces, or implementation details unless asked."
                    ),
                },
                {"role": "user", "content": json.dumps({"question": user_message, "analysis": result}, ensure_ascii=False, default=str)},
            ]
        )
        trace = {
            "tool_name": "python_analysis",
            "steps": steps + [{"name": "generate_answer", "input": {"question": user_message, "analysis_result": result}, "output": {"answer": answer}}],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": result,
            "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
            "native_function_call": True,
        }
        return {"answer": answer, "tool_name": "python_analysis", "tool_result": result, "trace": trace}

    sql = str(tool_args.get("sql") or "").strip()
    if not sql:
        sql = choose_local_sql(user_message, schema)
        steps.append({"name": "native_argument_repair", "source": "legacy_sql_generator", "output": {"sql": sql}})
    if " limit " not in sql.lower() and not re.search(r"\bcount\s*\(|\bsum\s*\(|\bavg\s*\(|\bmin\s*\(|\bmax\s*\(", sql, re.I):
        sql = f"{sql.rstrip(';')} limit 50"
    try:
        result = execute_local_sql(sql)
    except Exception as exc:
        repair_started = time.perf_counter()
        steps.append({"name": "execute_sql", "tool": "local_sql", "input": {"database": str(LOCAL_DB_PATH), "sql": sql}, "error": str(exc)})
        tool_name_2, tool_args_2, _ = choose_local_native_tool_call(user_message, schema, previous_error=str(exc))
        if tool_name_2 == "local_sql" and tool_args_2.get("sql"):
            sql = str(tool_args_2["sql"])
        else:
            sql = choose_local_sql(user_message, schema, previous_error=str(exc))
        if " limit " not in sql.lower() and not re.search(r"\bcount\s*\(|\bsum\s*\(|\bavg\s*\(|\bmin\s*\(|\bmax\s*\(", sql, re.I):
            sql = f"{sql.rstrip(';')} limit 50"
        result = execute_local_sql(sql)
        steps.append(
            {
                "name": "repair_sql",
                "tool": "ollama_native_tool_call",
                "input": {"previous_error": str(exc)},
                "output": {"sql": sql},
                "duration_ms": round((time.perf_counter() - repair_started) * 1000, 2),
            }
        )
    steps.append({"name": "execute_sql", "tool": "local_sql", "input": {"database": str(LOCAL_DB_PATH), "sql": sql}, "output": result})
    answer = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Answer the user's CRM question from the SQLite query result. "
                    "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
                    "Do not infer or invent data. Be concise. Do not mention SQL, query text, tool names, traces, or implementation details unless asked."
                ),
            },
            {"role": "user", "content": json.dumps({"question": user_message, "query_result": result}, ensure_ascii=False, default=str)},
        ]
    )
    trace = {
        "tool_name": "local_sql",
        "steps": steps + [{"name": "generate_answer", "input": {"question": user_message, "query_result": result}, "output": {"answer": answer}}],
        "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
        "tool_output": result,
        "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
        "native_function_call": True,
    }
    return {"answer": answer, "tool_name": "local_sql", "tool_result": result, "trace": trace}


def choose_tool(user_message: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    routed = route_obvious_tool(user_message, tools)
    if routed:
        return routed

    compact_tools = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        }
        for tool in tools
    ]
    content = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are choosing whether to call a Zoho Analytics MCP tool. "
                    "Choose only read-only tools by default. Do not choose create, update, delete, import, or mutation tools "
                    "unless the user explicitly requested that action. "
                    "Return only JSON with keys: use_tool boolean, tool_name string or null, "
                    "arguments object, reason string. Use a tool only when it is needed."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_message": user_message,
                        "available_tools": compact_tools,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        json_mode=True,
    )
    decision = parse_json_object(content)
    decision.setdefault("arguments", {})
    return decision


def route_obvious_tool(user_message: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    tool_names = {tool["name"] for tool in tools}
    text = user_message.lower()

    if "get_workspaces_list" in tool_names and "workspace" in text and any(
        word in text for word in ["list", "show", "get", "what", "which", "all"]
    ):
        return {
            "use_tool": True,
            "tool_name": "get_workspaces_list",
            "arguments": {"include_shared_workspaces": False, "contains_str": None},
            "reason": "User is asking to list Zoho Analytics workspaces.",
        }

    return None


@app.get("/")
def index():
    local_mode = DATA_SOURCE == "local"
    chat_id = ensure_chat_session(request.args.get("chat"))
    return render_template(
        "chat.html",
        model=OLLAMA_MODEL,
        chat_id=chat_id,
        data_source=DATA_SOURCE,
        title="Local Zoho CRM Chat" if local_mode else "Zoho Analytics MCP Chat",
        intro=(
            "Ask about the local synthetic CRM data in SQLite. Example: how many leads do we have?"
            if local_mode
            else "Ask about your Zoho Analytics workspaces, views, tables, or data. The app will let Ollama choose a Zoho MCP tool when needed."
        ),
        placeholder="Example: show 10 deals" if local_mode else "Example: list my workspaces",
        sidebar_title="Local CRM Tables" if local_mode else "Available MCP Tools",
    )


@app.get("/new-chat")
def new_chat():
    session_id = create_chat_session()
    return redirect(f"/?chat={session_id}")


@app.get("/data")
def data_page():
    return render_template("data.html", db_path=str(LOCAL_DB_PATH))


@app.get("/data/tables")
def data_tables():
    try:
        return jsonify({"ok": True, "tables": local_schema()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/data/rows")
def data_rows():
    try:
        table = request.args.get("table", "")
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
        return jsonify({"ok": True, **local_table_rows(table, limit, offset)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/tools")
def tools():
    try:
        if DATA_SOURCE == "local":
            return jsonify({"ok": True, "tools": local_tools()})
        return jsonify({"ok": True, "tools": mcp_list_tools()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/sessions")
def sessions():
    try:
        return jsonify({"ok": True, "sessions": list_chat_sessions()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/history")
def history():
    try:
        session_id = ensure_chat_session(request.args.get("chat"))
        return jsonify({"ok": True, "chat_id": session_id, "turns": load_chat_history(session_id)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/trace/<trace_id>")
def trace(trace_id: str):
    payload = load_trace(trace_id)
    if payload is None:
        return jsonify({"ok": False, "error": "Trace not found."}), 404
    return jsonify({"ok": True, "trace": payload})


@app.get("/trace-page/<trace_id>")
def trace_page(trace_id: str):
    payload = load_trace(trace_id)
    if payload is None:
        return "Trace not found.", 404
    steps = payload.get("steps") or []
    question = (
        payload.get("tool_input", {}).get("question")
        or next((step.get("input", {}).get("question") for step in steps if step.get("input", {}).get("question")), "")
    )
    return render_template(
        "trace.html",
        trace_id=trace_id,
        trace=payload,
        steps=steps,
        timings=payload.get("timings_ms") or {},
        question=question,
        raw_trace=json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        render_json=render_trace_value,
    )


@app.get("/feedback")
def feedback_page():
    return render_template(
        "feedback.html",
        items=load_disliked_feedback(),
        render_json=render_trace_value,
    )


@app.post("/feedback/dislike")
def dislike_feedback():
    trace_id = (request.json or {}).get("trace_id", "").strip()
    if not trace_id:
        return jsonify({"ok": False, "error": "trace_id is required."}), 400
    diagnostic = save_dislike_feedback(trace_id)
    if diagnostic is None:
        return jsonify({"ok": False, "error": "Response not found."}), 404
    return jsonify({"ok": True})


@app.post("/chat")
def chat():
    user_message = (request.json or {}).get("message", "").strip()
    session_id = ensure_chat_session((request.json or {}).get("chat_id"))
    if not user_message:
        return jsonify({"ok": False, "error": "Message is required."}), 400

    try:
        if DATA_SOURCE == "local":
            result = answer_from_local(user_message)
            trace_id = str(uuid.uuid4())
            trace_payload = result.get("trace") or {
                "tool_name": result.get("tool_name"),
                "tool_output": result.get("tool_result"),
            }
            save_chat_turn(
                session_id=session_id,
                trace_id=trace_id,
                user_message=user_message,
                answer=result["answer"],
                tool_name=result.get("tool_name"),
                trace=trace_payload,
            )
            return jsonify({"ok": True, "trace_id": trace_id, **result})

        started = time.perf_counter()
        available_tools = mcp_list_tools()
        decision = choose_tool(user_message, available_tools)
        tool_result = None
        tool_name = None

        if decision.get("use_tool"):
            tool_name = decision.get("tool_name")
            tool_names = {tool["name"] for tool in available_tools}
            if tool_name not in tool_names:
                raise RuntimeError(f"Ollama selected an unknown tool: {tool_name}")
            tool_result = mcp_call_tool(tool_name, decision.get("arguments") or {})
        tool_finished = time.perf_counter()

        answer = ollama_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Answer clearly and briefly. If tool data is provided, base the answer on it. "
                        "If the tool failed, explain the failure and the likely next setup step. "
                        "For Zoho Analytics token scope errors, recommend regenerating the refresh token "
                        "with ZohoAnalytics.fullaccess.all for testing, or narrower scopes such as "
                        "ZohoAnalytics.metadata.read, ZohoAnalytics.data.read, and ZohoAnalytics.modeling.create. "
                        "Do not invent Zoho scope names. Do not mention tool internals unless the user explicitly asks for them."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": user_message,
                            "tool_decision": decision,
                            "tool_result": tool_result,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        finished_at = time.perf_counter()
        trace_id = str(uuid.uuid4())
        trace_payload = {
            "tool_name": tool_name,
            "tool_input": decision,
            "tool_output": tool_result,
            "timings_ms": {
                "tool_selection_and_execution": round((tool_finished - started) * 1000, 2),
                "answer_generation": round((finished_at - tool_finished) * 1000, 2),
                "total": round((finished_at - started) * 1000, 2),
            },
        }
        save_chat_turn(
            session_id=session_id,
            trace_id=trace_id,
            user_message=user_message,
            answer=answer,
            tool_name=tool_name,
            trace=trace_payload,
        )
        return jsonify(
            {"ok": True, "answer": answer, "tool_name": tool_name, "tool_result": tool_result, "trace_id": trace_id}
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(ENV.get("PORT", "5000")), debug=True)
