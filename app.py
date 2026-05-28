import ast
import csv
import io
import json
import os
import re
import shutil
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
from flask import Flask, Response, jsonify, redirect, render_template, request


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
SETTINGS_PATH = Path(ENV.get("APP_SETTINGS_FILE", APP_DIR / "app_settings.json")).resolve()
SCENARIO_BACKUP_PATH = Path(ENV.get("SCENARIO_BACKUP_DB", APP_DIR / "zoho_crm_local.before_scenario.sqlite3")).resolve()
DEFAULT_SETTINGS = {
    "model": OLLAMA_MODEL,
    "ollama_url": OLLAMA_URL,
    "temperature": 0.1,
    "num_predict": 900,
    "data_source": DATA_SOURCE,
    "max_sql_rows": 40000,
    "language_mode": "match_user",
}
ADMIN_PASSWORD = "admin"

app = Flask(__name__)


def load_app_settings() -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update(saved)
        except json.JSONDecodeError:
            pass
    settings["temperature"] = max(0.0, min(float(settings.get("temperature", 0.1)), 2.0))
    settings["num_predict"] = max(128, min(int(settings.get("num_predict", 900)), 8192))
    settings["max_sql_rows"] = max(1, min(int(settings.get("max_sql_rows", 40000)), 40000))
    settings["data_source"] = str(settings.get("data_source") or "local").lower()
    settings["language_mode"] = str(settings.get("language_mode") or "match_user")
    settings["model"] = str(settings.get("model") or DEFAULT_SETTINGS["model"])
    settings["ollama_url"] = str(settings.get("ollama_url") or DEFAULT_SETTINGS["ollama_url"]).rstrip("/")
    return settings


def save_app_settings(settings: dict[str, Any]) -> dict[str, Any]:
    clean = DEFAULT_SETTINGS.copy()
    clean.update(
        {
            "model": str(settings.get("model") or DEFAULT_SETTINGS["model"]).strip(),
            "ollama_url": str(settings.get("ollama_url") or DEFAULT_SETTINGS["ollama_url"]).strip().rstrip("/"),
            "temperature": max(0.0, min(float(settings.get("temperature", 0.1)), 2.0)),
            "num_predict": max(128, min(int(settings.get("num_predict", 900)), 8192)),
            "data_source": str(settings.get("data_source") or "local").lower(),
            "max_sql_rows": max(1, min(int(settings.get("max_sql_rows", 40000)), 40000)),
            "language_mode": str(settings.get("language_mode") or "match_user"),
        }
    )
    SETTINGS_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def current_model() -> str:
    return load_app_settings()["model"]


def current_data_source() -> str:
    return load_app_settings()["data_source"]


def require_admin_password(payload: dict[str, Any]) -> tuple[bool, str | None]:
    password = str(payload.get("admin_password") or payload.get("password") or "")
    if password == ADMIN_PASSWORD:
        return True, None
    return False, "Admin password is required."


def language_instruction() -> str:
    mode = load_app_settings()["language_mode"]
    if mode == "arabic":
        return "Answer user-facing text in Arabic unless the user explicitly asks for another language. "
    if mode == "english":
        return "Answer user-facing text in English unless the user explicitly asks for another language. "
    return "Answer in the same language as the user's question. If the user writes Arabic, answer in Arabic. "






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
        if "feedback_reviewed" not in columns:
            con.execute("alter table chat_turns add column feedback_reviewed integer not null default 0")
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
                current_data_source(),
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


def load_disliked_feedback(
    limit: int = 100,
    tool: str = "",
    severity: str = "",
    status: str = "open",
    query: str = "",
) -> list[dict[str, Any]]:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        filters = ["disliked = 1"]
        params: list[Any] = []
        if tool:
            filters.append("coalesce(tool_name, '') = ?")
            params.append(tool)
        if status == "reviewed":
            filters.append("feedback_reviewed = 1")
        elif status == "all":
            pass
        else:
            filters.append("feedback_reviewed = 0")
        if query:
            filters.append("(user_message like ? or answer like ? or feedback_json like ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        where = " and ".join(filters)
        rows = con.execute(
            f"""
            select id, created_at, feedback_created_at, user_message, answer, tool_name,
                   feedback_json, feedback_reviewed
            from chat_turns
            where {where}
            order by feedback_created_at desc, created_at desc
            limit ?
            """,
            (*params, limit),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["feedback"] = json.loads(item.get("feedback_json") or "{}")
            except json.JSONDecodeError:
                item["feedback"] = {"raw": item.get("feedback_json")}
            item["reviewed"] = bool(item.get("feedback_reviewed"))
            if severity and str(item["feedback"].get("severity", "")).lower() != severity.lower():
                continue
            items.append(item)
        return items
    finally:
        con.close()


def disliked_feedback_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_issue: dict[str, dict[str, Any]] = {}
    reviewed = 0
    for item in items:
        tool = item.get("tool_name") or "none"
        by_tool[tool] = by_tool.get(tool, 0) + 1
        if item.get("reviewed"):
            reviewed += 1
        feedback = item.get("feedback") or {}
        severity = str(feedback.get("severity") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        issue = str(feedback.get("likely_issue") or "Uncategorized")
        fix = str(feedback.get("prompt_or_routing_fix") or feedback.get("better_action") or "")
        group = by_issue.setdefault(issue, {"issue": issue, "count": 0, "fixes": set()})
        group["count"] += 1
        if fix:
            group["fixes"].add(fix)
    issue_groups = []
    for group in by_issue.values():
        issue_groups.append(
            {
                "issue": group["issue"],
                "count": group["count"],
                "fixes": sorted(group["fixes"])[:3],
            }
        )
    issue_groups.sort(key=lambda group: group["count"], reverse=True)
    return {
        "total": len(items),
        "open": len(items) - reviewed,
        "reviewed": reviewed,
        "by_tool": sorted(by_tool.items(), key=lambda item: item[1], reverse=True),
        "by_severity": sorted(by_severity.items(), key=lambda item: item[1], reverse=True),
        "issue_groups": issue_groups[:8],
    }


def feedback_filter_options() -> dict[str, list[str]]:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        tools = [
            row[0] or "none"
            for row in con.execute(
                "select distinct coalesce(tool_name, 'none') from chat_turns where disliked = 1 order by 1"
            ).fetchall()
        ]
        severities = set()
        for row in con.execute("select feedback_json from chat_turns where disliked = 1 and feedback_json is not null"):
            try:
                severity = str(json.loads(row[0]).get("severity") or "")
            except json.JSONDecodeError:
                severity = ""
            if severity:
                severities.add(severity)
        return {"tools": tools, "severities": sorted(severities)}
    finally:
        con.close()


def set_feedback_reviewed(trace_id: str, reviewed: bool) -> bool:
    init_history_db()
    con = sqlite3.connect(HISTORY_DB_PATH)
    try:
        cur = con.execute(
            "update chat_turns set feedback_reviewed = ? where id = ? and disliked = 1",
            (1 if reviewed else 0, trace_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


GOLDEN_TESTS = [
    {"id": "block_capital", "category": "Blockers", "question": "ما عاصمة فرنسا؟", "tools": ["unsupported_question"], "required": ["CRM"]},
    {"id": "block_president", "category": "Blockers", "question": "من هو رئيس الولايات المتحدة؟", "tools": ["unsupported_question"], "required": ["CRM"]},
    {"id": "block_sports", "category": "Blockers", "question": "ما آخر مباراة في الدوري الإنجليزي الممتاز؟", "tools": ["unsupported_question"], "required": ["CRM"]},
    {"id": "block_weather", "category": "Blockers", "question": "ما حالة الطقس في دبي غداً؟", "tools": ["unsupported_question"], "required": ["CRM"]},
    {"id": "block_joke", "category": "Blockers", "question": "اكتب لي نكتة قصيرة", "tools": ["unsupported_question"], "required": ["CRM"]},
    {"id": "lead_count", "category": "Simple", "question": "كم عدد العملاء المحتملين لدينا؟", "tools": ["local_sql"], "required": ["200"]},
    {"id": "sales_revenue", "category": "Simple", "question": "ما إجمالي إيرادات المبيعات لدينا؟", "tools": ["local_sql"], "required": []},
    {"id": "overdue_payments", "category": "Simple", "question": "اعرض لي المدفوعات المتأخرة", "tools": ["local_sql"], "required": ["|"]},
    {"id": "account_count", "category": "Simple", "question": "كم عدد الحسابات لدينا؟", "tools": ["local_sql"], "required": ["200"]},
    {"id": "average_deal", "category": "Simple", "question": "ما متوسط قيمة الصفقة؟", "tools": ["local_sql"], "required": []},
    {"id": "pipeline_weakest", "category": "Complex", "question": "حلل مسار المبيعات وحدد أضعف مرحلة", "tools": ["python_analysis"], "required": []},
    {"id": "industry_focus", "category": "Complex", "question": "قارن العملاء المحتملين حسب القطاع وأخبرني أين يجب أن نركز", "tools": ["local_sql", "python_analysis"], "required": []},
    {"id": "top_accounts", "category": "Complex", "question": "حدد أفضل الحسابات أداء ومساهمتها في الإيرادات", "tools": ["local_sql", "python_analysis"], "required": ["%"]},
    {"id": "won_lost", "category": "Complex", "question": "قارن الصفقات الرابحة والخاسرة حسب القيمة والعدد", "tools": ["local_sql", "python_analysis"], "required": ["Closed"]},
    {"id": "owner_revenue", "category": "Complex", "question": "حلل الإيرادات حسب مالك الصفقة وحدد أفضل ثلاثة مالكين", "tools": ["local_sql", "python_analysis"], "required": []},
    {"id": "campaign_clarify", "category": "Clarification", "question": "أي حملة أفضل؟", "tools": ["clarification_needed"], "required": ["؟"]},
    {"id": "team_clarify", "category": "Clarification", "question": "قارن الأداء بين الفرق", "tools": ["clarification_needed"], "required": ["مقياس"]},
    {"id": "employee_clarify", "category": "Clarification", "question": "من هو أفضل موظف؟", "tools": ["clarification_needed"], "required": ["معيار"]},
    {"id": "employee_departments", "category": "Employees", "question": "كم عدد الموظفين في كل قسم؟", "tools": ["local_sql"], "required": ["|"]},
    {"id": "salary_roles", "category": "Employees", "question": "ما متوسط الراتب حسب الدور الوظيفي؟", "tools": ["local_sql"], "required": ["|"]},
    {"id": "salary_departments", "category": "Employees", "question": "حدد أعلى الأقسام من حيث إجمالي الرواتب", "tools": ["local_sql"], "required": ["|"]},
    {"id": "activity_types", "category": "Activities", "question": "ما أكثر أنواع الأنشطة شيوعاً في CRM؟", "tools": ["local_sql"], "required": ["Call"]},
    {"id": "call_owner", "category": "Activities", "question": "من يملك أكبر عدد من المكالمات؟", "tools": ["local_sql"], "required": []},
    {"id": "overdue_tasks", "category": "Activities", "question": "اعرض المهام المتأخرة حسب الأولوية", "tools": ["local_sql"], "required": ["|"]},
]


SCENARIOS = [
    {
        "id": "bad_quarter",
        "name": "Bad Quarter",
        "description": "Lower close quality, more lost deals, slower cycles, and weaker pipeline health.",
        "questions": [
            "حلل مسار المبيعات وحدد أضعف مرحلة",
            "قارن الصفقات الرابحة والخاسرة حسب القيمة والعدد",
            "ما إجمالي إيرادات المبيعات لدينا؟",
        ],
    },
    {
        "id": "high_overdue_invoices",
        "name": "High Overdue Invoices",
        "description": "Many invoices become old, unpaid, and high-balance so payment risk is easy to detect.",
        "questions": [
            "اعرض لي المدفوعات المتأخرة",
            "ما إجمالي إيرادات المبيعات لدينا؟",
        ],
    },
    {
        "id": "strong_marketing_campaign",
        "name": "Strong Marketing Campaign",
        "description": "Campaign metrics and related won deals improve to create a strong marketing story.",
        "questions": [
            "أي حملة أفضل؟",
            "حدد أفضل الحسابات أداء ومساهمتها في الإيرادات",
            "قارن العملاء المحتملين حسب القطاع وأخبرني أين يجب أن نركز",
        ],
    },
]


def evaluate_answer(question: str, expected: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    answer = str(result.get("answer") or "")
    tool = str(result.get("tool_name") or "")
    expected_tools = expected.get("tools") or []
    required = expected.get("required") or []
    forbidden = expected.get("forbidden") or ["```", "<tool_call>"]
    failures = []
    if expected_tools and tool not in expected_tools:
        failures.append(f"Expected tool one of {expected_tools}, got {tool or 'none'}.")
    for term in required:
        if term and term.lower() not in answer.lower():
            failures.append(f"Missing expected text: {term}")
    for term in forbidden:
        if term and term.lower() in answer.lower():
            failures.append(f"Unexpected text: {term}")
    if uses_arabic(question) and not uses_arabic(answer):
        failures.append("Expected Arabic answer for Arabic question.")
    return {
        "ok": not failures,
        "failures": failures,
        "tool": tool,
        "answer": answer,
        "steps": [step.get("name") for step in (result.get("trace") or {}).get("steps", [])],
    }


def run_agent_without_history(question: str) -> dict[str, Any]:
    if current_data_source() != "local":
        raise RuntimeError("Golden tests and scenarios run against the local CRM database only.")
    return answer_from_local(question)


def scenario_metrics() -> dict[str, Any]:
    con = sqlite3.connect(LOCAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return {
            "deals_by_stage": [dict(row) for row in con.execute("select Stage, count(*) Count, round(sum(Amount), 2) Amount from Deals group by Stage order by Amount desc")],
            "overdue_invoices": dict(con.execute("select count(*) Count, round(sum(Balance), 2) Balance from Invoices where date(Due_Date) < date('now') and Balance > 0 and lower(Invoice_Status) not in ('cancelled', 'draft')").fetchone()),
            "campaigns": [dict(row) for row in con.execute("select Campaign_Name, Status, round(Expected_Revenue, 2) Expected_Revenue, round(Actual_Cost, 2) Actual_Cost, Expected_Response from Campaigns order by Expected_Revenue desc limit 5")],
        }
    finally:
        con.close()


def apply_scenario(scenario_id: str) -> dict[str, Any]:
    if not LOCAL_DB_PATH.exists():
        raise RuntimeError("Local CRM database does not exist.")
    if not SCENARIO_BACKUP_PATH.exists():
        shutil.copy2(LOCAL_DB_PATH, SCENARIO_BACKUP_PATH)
    con = sqlite3.connect(LOCAL_DB_PATH)
    try:
        if scenario_id == "bad_quarter":
            con.execute("update Deals set Stage = 'Closed Lost', Probability = 10, Sales_Cycle_Duration = 620 where cast(substr(id, -2) as integer) % 3 = 0")
            con.execute("update Deals set Stage = 'Proposal', Probability = 35, Sales_Cycle_Duration = 540 where cast(substr(id, -2) as integer) % 3 = 1")
            con.execute("update Deals set Amount = round(Amount * 0.62, 2), Expected_Revenue = round(Expected_Revenue * 0.55, 2)")
            con.execute("update Leads set Lead_Status = 'Lost' where cast(substr(id, -2) as integer) % 4 = 0")
        elif scenario_id == "high_overdue_invoices":
            con.execute("update Invoices set Invoice_Status = 'Confirmed', Due_Date = date('now', '-180 days'), Balance = round(Grand_Total * 0.72, 2) where cast(substr(id, -2) as integer) % 2 = 0")
            con.execute("update Invoices set Invoice_Status = 'Delivered', Due_Date = date('now', '-90 days'), Balance = round(Grand_Total * 0.45, 2) where cast(substr(id, -2) as integer) % 2 = 1")
        elif scenario_id == "strong_marketing_campaign":
            con.execute("update Campaigns set Status = 'Active', Campaign_Name = 'Enterprise Webinar', Expected_Revenue = 950000, Actual_Cost = 42000, Budgeted_Cost = 65000, Expected_Response = 84 where rowid <= 60")
            con.execute("update Deals set Stage = 'Closed Won', Amount = round(Amount * 1.75, 2), Expected_Revenue = round(Amount * 1.55, 2), Probability = 95, Campaign_Source = 'Enterprise Webinar' where cast(substr(id, -2) as integer) % 2 = 0")
            con.execute("update Leads set Lead_Source = 'Webinar', Lead_Status = 'Qualified', Annual_Revenue = round(Annual_Revenue * 1.6, 2) where cast(substr(id, -2) as integer) % 2 = 0")
        else:
            raise RuntimeError("Unknown scenario.")
        con.commit()
    finally:
        con.close()
    return scenario_metrics()


def reset_scenario_database() -> bool:
    if not SCENARIO_BACKUP_PATH.exists():
        return False
    shutil.copy2(SCENARIO_BACKUP_PATH, LOCAL_DB_PATH)
    return True


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
    settings = load_app_settings()
    payload: dict[str, Any] = {
        "model": settings["model"],
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {
            "temperature": settings["temperature"],
            "num_predict": settings["num_predict"],
        },
    }
    if json_mode:
        payload["format"] = "json"
    if tools:
        payload["tools"] = tools
    response = requests.post(f"{settings['ollama_url']}/api/chat", json=payload, timeout=300)
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
                                "GROUP BY may contain only raw non-aggregate columns. For non-aggregate row lists, include a LIMIT no higher than the configured max SQL rows. "
                                "For overdue invoice/payment lists, return Invoice_Status, Subject, Account_Name, Due_Date, Grand_Total, Balance."
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
                    "Use this for multi-table analysis, ranking, recommendations, risk assessment, or complex comparisons. "
                    "For sales-pipeline weakest-stage analysis, load all Deal rows without SQL GROUP BY, exclude Closed Won/Closed Lost, "
                    "compute value_at_risk = Amount * (1 - Probability/100), then stage risk_score = sum(value_at_risk) * (1 + avg(Sales_Cycle_Duration)/365). "
                    "Rank open stages by total risk_score descending, not by per-deal risk."
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
        "If the user asks which campaign, employee, team, account, or group is best/better without naming a metric, "
        "call clarification_needed and ask whether to compare by revenue, cost, count, conversion, activity volume, or another metric. "
        + language_instruction()
        + "For every tool argument that contains user-facing text, follow the language behavior above. "
        "For unsupported_question and clarification_needed, write the reason/question in the same language as the user. "
        "For simple counts/totals/lists use local_sql. For multi-step analysis, rankings, recommendations, risk, focus, or comparisons use python_analysis. "
        "When generating python_analysis code, prefer loading raw rows with SELECT column lists and doing grouping in pandas/Polars. "
        "Do not use SQL GROUP BY unless every selected non-aggregate column is included in GROUP BY. "
        "Do not use LIMIT 50 by habit. If the user asks for row-level data and does not specify a smaller number, use the configured max SQL rows. "
        "For weakest sales-pipeline stage, do not infer funnel transitions from stage order; compute stage risk_score = sum(value_at_risk) * (1 + avg(Sales_Cycle_Duration)/365) "
        "for open Deals stages and rank by total risk_score descending, not by per-deal averages. "
        "For overdue payments use local_sql on Invoices and select Invoice_Status, Subject, Account_Name, Due_Date, Grand_Total, Balance; "
        "filter date(Due_Date) < date('now'), Balance > 0, Balance <= Grand_Total, and exclude cancelled/draft invoice statuses. "
        "For total sales revenue, use local_sql and sum Deals.Amount across all Deals; do not exclude Closed Lost or any stage unless the user explicitly asks for won/open revenue. "
        "For top accounts by revenue contribution, local_sql can group Deals by Account_Name and compute SUM(Amount), COUNT(*), "
        "and revenue contribution as SUM(Amount) * 100.0 / (SELECT SUM(Amount) FROM Deals). "
        "For industry focus, compare Leads or Accounts by Industry using available revenue/count fields. "
        "Calls table links to people via Who_Id and has Owner_Name for call owner analysis; do not join Calls on Account_Name. "
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
        think=current_model().lower().startswith("qwen"),
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


def uses_arabic(text: str) -> bool:
    return any("\u0600" <= char <= "\u06ff" for char in text)


def localized_unsupported_answer(user_message: str, reason: str) -> str:
    if uses_arabic(user_message):
        return "لا أستطيع الإجابة عن هذا من قاعدة بيانات CRM المحلية. السؤال خارج نطاق بيانات CRM المحلية."
    return f"I can't answer that from the local CRM database. {reason}"


def localized_clarification_answer(user_message: str, question: str) -> str:
    if uses_arabic(user_message):
        return question if uses_arabic(question) else "أحتاج إلى توضيح أكثر: ما المقياس أو النطاق الذي تريد تحليله؟"
    return question


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


def execute_local_sql(sql: str) -> dict[str, Any]:
    if not is_read_only_sql(sql):
        raise RuntimeError("Only read-only SELECT queries are allowed.")
    max_rows = load_app_settings()["max_sql_rows"]
    con = sqlite3.connect(LOCAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql).fetchmany(max_rows)
        return {"sql": sql, "rows": [dict(row) for row in rows], "row_count": len(rows)}
    finally:
        con.close()


def expand_stale_default_limit(sql: str, user_message: str) -> str:
    max_rows = load_app_settings()["max_sql_rows"]
    if max_rows <= 50 or re.search(r"\b50\b", user_message):
        return sql
    return re.sub(r"\blimit\s+50\b", f"limit {max_rows}", sql, flags=re.I)


def has_overdue_invoice_shape(result: dict[str, Any]) -> bool:
    rows = result.get("rows") or []
    if not rows:
        sql = str(result.get("sql") or "").lower()
        return "invoices" in sql and "due_date" in sql and "balance" in sql
    columns = set(rows[0])
    return {"Invoice_Status", "Subject", "Account_Name", "Due_Date", "Grand_Total", "Balance"}.issubset(columns)


def format_overdue_payments_answer(result: dict[str, Any], user_message: str) -> str:
    rows = []
    for row in result.get("rows") or []:
        status = str(row.get("Invoice_Status") or "").lower()
        grand_total = float(row.get("Grand_Total") or 0)
        balance = float(row.get("Balance") or 0)
        if status in {"cancelled", "draft"} or balance <= 0 or grand_total <= 0 or balance > grand_total:
            continue
        rows.append(row)
    rows = sorted(rows, key=lambda row: str(row.get("Due_Date") or ""))[:10]
    if uses_arabic(user_message):
        if not rows:
            return "لا توجد مدفوعات متأخرة حسب البيانات المحلية الحالية."
        lines = [
            "هذه أقدم المدفوعات المتأخرة غير الملغاة التي لديها رصيد متبق:",
            "",
            "| حالة الفاتورة | الموضوع | الحساب | تاريخ الاستحقاق | الإجمالي | الرصيد المتبقي |",
            "| :--- | :--- | :--- | :--- | ---: | ---: |",
        ]
        for row in rows:
            lines.append(
                "| {status} | {subject} | {account} | {due} | {grand:,.2f} | {balance:,.2f} |".format(
                    status=row.get("Invoice_Status", ""),
                    subject=row.get("Subject", ""),
                    account=row.get("Account_Name", ""),
                    due=row.get("Due_Date", ""),
                    grand=float(row.get("Grand_Total") or 0),
                    balance=float(row.get("Balance") or 0),
                )
            )
        lines.append("")
        lines.append("تم استبعاد الفواتير الملغاة والمسودات، والترتيب حسب أقدم تاريخ استحقاق.")
        return "\n".join(lines)
    if not rows:
        return "There are no overdue payments in the current local data."
    lines = [
        "These are the oldest non-cancelled overdue payments with a remaining balance:",
        "",
        "| Invoice Status | Subject | Account | Due Date | Grand Total | Balance |",
        "| :--- | :--- | :--- | :--- | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {status} | {subject} | {account} | {due} | {grand:,.2f} | {balance:,.2f} |".format(
                status=row.get("Invoice_Status", ""),
                subject=row.get("Subject", ""),
                account=row.get("Account_Name", ""),
                due=row.get("Due_Date", ""),
                grand=float(row.get("Grand_Total") or 0),
                balance=float(row.get("Balance") or 0),
            )
        )
    lines.append("")
    lines.append("Cancelled and draft invoices are excluded, sorted by oldest due date.")
    return "\n".join(lines)


def has_task_list_shape(result: dict[str, Any]) -> bool:
    rows = result.get("rows") or []
    if not rows:
        sql = str(result.get("sql") or "").lower()
        return "tasks" in sql and "due_date" in sql and "priority" in sql
    columns = set(rows[0])
    return {"Subject", "Priority", "Due_Date", "Status"}.issubset(columns)


def format_task_list_answer(result: dict[str, Any], user_message: str) -> str:
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    rows = sorted(
        result.get("rows") or [],
        key=lambda row: (
            priority_rank.get(str(row.get("Priority") or "").lower(), 9),
            str(row.get("Due_Date") or ""),
        ),
    )[:10]
    owner_key = "Owner_Name" if rows and "Owner_Name" in rows[0] else "Owner"
    if uses_arabic(user_message):
        if not rows:
            return "لا توجد مهام متأخرة حسب البيانات المحلية الحالية."
        lines = [
            "هذه أهم المهام المتأخرة مرتبة حسب الأولوية ثم تاريخ الاستحقاق:",
            "",
            "| المهمة | الأولوية | تاريخ الاستحقاق | الحالة | المالك |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for row in rows:
            lines.append(
                "| {subject} | {priority} | {due} | {status} | {owner} |".format(
                    subject=row.get("Subject", ""),
                    priority=row.get("Priority", ""),
                    due=row.get("Due_Date", ""),
                    status=row.get("Status", ""),
                    owner=row.get(owner_key, ""),
                )
            )
        return "\n".join(lines)
    if not rows:
        return "There are no overdue tasks in the current local data."
    lines = [
        "These are the highest-priority overdue tasks, sorted by priority and due date:",
        "",
        "| Task | Priority | Due Date | Status | Owner |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for row in rows:
        lines.append(
            "| {subject} | {priority} | {due} | {status} | {owner} |".format(
                subject=row.get("Subject", ""),
                priority=row.get("Priority", ""),
                due=row.get("Due_Date", ""),
                status=row.get("Status", ""),
                owner=row.get(owner_key, ""),
            )
        )
    return "\n".join(lines)


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
    ]
    for pattern in blocked:
        if re.search(pattern, code):
            raise RuntimeError(f"Python script contains a blocked operation: {pattern}")
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if re.match(r"^(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum)\b", text, re.I):
                raise RuntimeError("Python script contains mutating SQL.")


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
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
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
        raise RuntimeError(f"Ollama did not choose a valid tool call: {exc}") from exc
    steps.append(
        {
            "name": "native_function_call",
            "tool": "ollama_native_tool_call",
            "reason": native_response.get("fallback_reason")
            or "Ollama returned message.tool_calls or a Qwen XML tool call.",
            "input": {"question": user_message, "available_tools": [tool["function"]["name"] for tool in local_function_tools()]},
            "output": {"tool_name": tool_name, "arguments": tool_args},
            "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2),
        }
    )

    if tool_name == "clarification_needed":
        answer = localized_clarification_answer(
            user_message,
            tool_args.get("question") or "Please clarify what metric or scope you want me to use.",
        )
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
        answer = localized_unsupported_answer(user_message, reason)
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
            raise RuntimeError("Native tool call selected python_analysis without code.")
        try:
            validate_python_script(code)
            result = run_python_analysis(code)
        except Exception as exc:
            repair_started = time.perf_counter()
            steps.append({"name": "execute_python_analysis", "input": {"database": str(LOCAL_DB_PATH), "code": code}, "error": str(exc)})
            tool_name_2, tool_args_2, _ = choose_local_native_tool_call(user_message, schema, previous_error=str(exc))
            if tool_name_2 != "python_analysis" or not tool_args_2.get("code"):
                raise RuntimeError("Ollama did not repair the python_analysis call.")
            else:
                code = str(tool_args_2["code"])
                repair_source = "ollama_native_tool_call"
            try:
                validate_python_script(code)
                result = run_python_analysis(code)
            except Exception:
                raise
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
                        + language_instruction()
                        + "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
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
        raise RuntimeError("Native tool call selected local_sql without sql.")
    sql = expand_stale_default_limit(sql, user_message)
    if " limit " not in sql.lower() and not re.search(r"\bcount\s*\(|\bsum\s*\(|\bavg\s*\(|\bmin\s*\(|\bmax\s*\(", sql, re.I):
        sql = f"{sql.rstrip(';')} limit {load_app_settings()['max_sql_rows']}"
    try:
        result = execute_local_sql(sql)
    except Exception as exc:
        repair_started = time.perf_counter()
        steps.append({"name": "execute_sql", "tool": "local_sql", "input": {"database": str(LOCAL_DB_PATH), "sql": sql}, "error": str(exc)})
        tool_name_2, tool_args_2, _ = choose_local_native_tool_call(user_message, schema, previous_error=str(exc))
        if tool_name_2 == "local_sql" and tool_args_2.get("sql"):
            sql = str(tool_args_2["sql"])
        else:
            raise RuntimeError("Ollama did not repair the local_sql call.")
        sql = expand_stale_default_limit(sql, user_message)
        if " limit " not in sql.lower() and not re.search(r"\bcount\s*\(|\bsum\s*\(|\bavg\s*\(|\bmin\s*\(|\bmax\s*\(", sql, re.I):
            sql = f"{sql.rstrip(';')} limit {load_app_settings()['max_sql_rows']}"
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
    if has_overdue_invoice_shape(result):
        answer = format_overdue_payments_answer(result, user_message)
        trace = {
            "tool_name": "local_sql",
            "steps": steps + [
                {
                    "name": "format_overdue_payments_answer",
                    "input": {"question": user_message, "query_result": result},
                    "output": {"answer": answer},
                }
            ],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": result,
            "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
            "native_function_call": True,
        }
        return {"answer": answer, "tool_name": "local_sql", "tool_result": result, "trace": trace}
    if has_task_list_shape(result):
        answer = format_task_list_answer(result, user_message)
        trace = {
            "tool_name": "local_sql",
            "steps": steps + [
                {
                    "name": "format_task_list_answer",
                    "input": {"question": user_message, "query_result": result},
                    "output": {"answer": answer},
                }
            ],
            "tool_input": {"question": user_message, "database": str(LOCAL_DB_PATH)},
            "tool_output": result,
            "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 2)},
            "native_function_call": True,
        }
        return {"answer": answer, "tool_name": "local_sql", "tool_result": result, "trace": trace}
    answer = ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Answer the user's CRM question from the SQLite query result. "
                    + language_instruction()
                    + "If the result does not contain enough evidence to answer the user's question, say exactly what is missing. "
                    "Do not infer or invent data. For row lists, summarize the important rows in a short markdown table. "
                    "Do not output raw JSON or code blocks unless the user explicitly asks for raw data. "
                    "Be concise. Do not mention SQL, query text, tool names, traces, or implementation details unless asked."
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
    data_source = current_data_source()
    local_mode = data_source == "local"
    chat_id = ensure_chat_session(request.args.get("chat"))
    return render_template(
        "chat.html",
        model=current_model(),
        chat_id=chat_id,
        data_source=data_source,
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


@app.get("/golden")
def golden_page():
    categories = sorted({test["category"] for test in GOLDEN_TESTS})
    return render_template("golden.html", tests=GOLDEN_TESTS, categories=categories)


@app.post("/golden/run")
def golden_run():
    payload = request.json or {}
    allowed, error = require_admin_password(payload)
    if not allowed:
        return jsonify({"ok": False, "error": error}), 403
    selected_ids = set(payload.get("ids") or [])
    tests = [test for test in GOLDEN_TESTS if not selected_ids or test["id"] in selected_ids]
    results = []
    for test in tests:
        started = time.perf_counter()
        try:
            result = run_agent_without_history(test["question"])
            evaluation = evaluate_answer(test["question"], test, result)
            results.append(
                {
                    "id": test["id"],
                    "category": test["category"],
                    "question": test["question"],
                    **evaluation,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": test["id"],
                    "category": test["category"],
                    "question": test["question"],
                    "ok": False,
                    "tool": None,
                    "steps": [],
                    "answer": "",
                    "failures": [str(exc)],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
    return jsonify(
        {
            "ok": True,
            "results": results,
            "summary": {
                "total": len(results),
                "passed": sum(1 for item in results if item["ok"]),
                "failed": sum(1 for item in results if not item["ok"]),
            },
        }
    )


@app.get("/scenarios")
def scenarios_page():
    return render_template(
        "scenarios.html",
        scenarios=SCENARIOS,
        has_backup=SCENARIO_BACKUP_PATH.exists(),
        metrics=scenario_metrics() if LOCAL_DB_PATH.exists() else {},
    )


@app.post("/scenarios/apply")
def scenarios_apply():
    scenario_id = (request.json or {}).get("scenario_id", "").strip()
    scenario = next((item for item in SCENARIOS if item["id"] == scenario_id), None)
    if not scenario:
        return jsonify({"ok": False, "error": "Unknown scenario."}), 404
    try:
        return jsonify({"ok": True, "scenario": scenario, "metrics": apply_scenario(scenario_id)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/scenarios/reset")
def scenarios_reset():
    try:
        restored = reset_scenario_database()
        return jsonify({"ok": True, "restored": restored, "metrics": scenario_metrics() if LOCAL_DB_PATH.exists() else {}})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/scenarios/test")
def scenarios_test():
    scenario_id = (request.json or {}).get("scenario_id", "").strip()
    scenario = next((item for item in SCENARIOS if item["id"] == scenario_id), None)
    if not scenario:
        return jsonify({"ok": False, "error": "Unknown scenario."}), 404
    results = []
    for question in scenario["questions"]:
        started = time.perf_counter()
        try:
            result = run_agent_without_history(question)
            results.append(
                {
                    "question": question,
                    "ok": True,
                    "tool": result.get("tool_name"),
                    "steps": [step.get("name") for step in (result.get("trace") or {}).get("steps", [])],
                    "answer": result.get("answer"),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except Exception as exc:
            results.append({"question": question, "ok": False, "error": str(exc)})
    return jsonify({"ok": True, "scenario": scenario, "results": results})


@app.get("/settings")
def settings_page():
    return render_template("settings.html", settings=load_app_settings(), settings_path=str(SETTINGS_PATH))


@app.post("/settings")
def settings_save():
    try:
        payload = request.json or request.form.to_dict()
        allowed, error = require_admin_password(payload)
        if not allowed:
            return jsonify({"ok": False, "error": error}), 403
        settings = save_app_settings(payload)
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/tools")
def tools():
    try:
        if current_data_source() == "local":
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
    filters = {
        "tool": request.args.get("tool", "").strip(),
        "severity": request.args.get("severity", "").strip(),
        "status": request.args.get("status", "open").strip() or "open",
        "query": request.args.get("query", "").strip(),
    }
    items = load_disliked_feedback(
        tool=filters["tool"],
        severity=filters["severity"],
        status=filters["status"],
        query=filters["query"],
        limit=250,
    )
    return render_template(
        "feedback.html",
        items=items,
        summary=disliked_feedback_summary(items),
        filters=filters,
        options=feedback_filter_options(),
        render_json=render_trace_value,
    )


@app.get("/feedback/export.<fmt>")
def feedback_export(fmt: str):
    filters = {
        "tool": request.args.get("tool", "").strip(),
        "severity": request.args.get("severity", "").strip(),
        "status": request.args.get("status", "all").strip() or "all",
        "query": request.args.get("query", "").strip(),
    }
    items = load_disliked_feedback(
        tool=filters["tool"],
        severity=filters["severity"],
        status=filters["status"],
        query=filters["query"],
        limit=1000,
    )
    if fmt == "json":
        return jsonify({"ok": True, "items": items, "summary": disliked_feedback_summary(items)})
    if fmt != "csv":
        return jsonify({"ok": False, "error": "Unsupported export format."}), 404
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "feedback_created_at",
            "tool_name",
            "reviewed",
            "severity",
            "likely_issue",
            "better_action",
            "prompt_or_routing_fix",
            "data_or_tool_gap",
            "user_message",
            "answer",
        ],
    )
    writer.writeheader()
    for item in items:
        feedback = item.get("feedback") or {}
        writer.writerow(
            {
                "id": item.get("id"),
                "feedback_created_at": item.get("feedback_created_at"),
                "tool_name": item.get("tool_name"),
                "reviewed": item.get("reviewed"),
                "severity": feedback.get("severity"),
                "likely_issue": feedback.get("likely_issue"),
                "better_action": feedback.get("better_action"),
                "prompt_or_routing_fix": feedback.get("prompt_or_routing_fix"),
                "data_or_tool_gap": feedback.get("data_or_tool_gap"),
                "user_message": item.get("user_message"),
                "answer": item.get("answer"),
            }
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=disliked-feedback.csv"},
    )


@app.post("/feedback/<trace_id>/reviewed")
def feedback_reviewed(trace_id: str):
    reviewed = bool((request.json or {}).get("reviewed", True))
    if not set_feedback_reviewed(trace_id, reviewed):
        return jsonify({"ok": False, "error": "Feedback not found."}), 404
    return jsonify({"ok": True, "reviewed": reviewed})


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
        if current_data_source() == "local":
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
            return jsonify({"ok": True, "chat_id": session_id, "trace_id": trace_id, **result})

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
            {
                "ok": True,
                "chat_id": session_id,
                "answer": answer,
                "tool_name": tool_name,
                "tool_result": tool_result,
                "trace_id": trace_id,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(ENV.get("PORT", "5000")), debug=True)
