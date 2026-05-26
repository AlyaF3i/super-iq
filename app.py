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
from flask import Flask, jsonify, redirect, render_template_string, request


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


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; --navy: #061427; --blue: #149ee7; --cyan: #2fd3ff; --ink: #101c2e; --line: #d7e5ee; --soft: #f3f8fb; }
    body { margin: 0; background: linear-gradient(180deg, #eef7fc 0%, #f7fbfe 46%, #edf4f8 100%); color: var(--ink); }
    .topbar { position: relative; overflow: hidden; background: radial-gradient(circle at 56% 0%, rgba(47, 211, 255, 0.22), transparent 34%), linear-gradient(135deg, #020915 0%, #061427 54%, #0a2742 100%); color: white; border-bottom: 4px solid var(--blue); }
    .topbar::after { content: ""; position: absolute; inset: 0; background: repeating-linear-gradient(135deg, transparent 0 18px, rgba(47, 211, 255, 0.72) 18px 26px, transparent 26px 44px); width: 190px; left: auto; opacity: 0.85; }
    .topbar-inner { position: relative; z-index: 1; max-width: 1320px; margin: 0 auto; padding: 16px 28px 20px; display: grid; grid-template-columns: 210px auto minmax(0, 1fr); gap: 24px; align-items: center; }
    .brand { font-weight: 900; font-size: 24px; line-height: 1; letter-spacing: 0; }
    .brand span { color: var(--cyan); }
    .hero-title { font-size: 28px; font-weight: 800; line-height: 1.16; text-shadow: 0 2px 8px rgba(0, 0, 0, 0.36); }
    .hero-title span { color: var(--cyan); }
    .status { font-size: 13px; color: #52616b; text-align: right; white-space: nowrap; }
    main { max-width: 1320px; margin: 0 auto; padding: 24px 28px 32px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: center; margin-bottom: 18px; }
    h1 { font-size: 22px; line-height: 1.2; margin: 0; }
    .shell { display: grid; grid-template-columns: 240px minmax(620px, 1fr) 340px; gap: 18px; align-items: start; }
    .panel { background: rgba(255, 255, 255, 0.96); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 14px 36px rgba(7, 33, 54, 0.08); }
    #messages { height: 66vh; min-height: 460px; overflow: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
    .msg { max-width: 86%; padding: 12px 14px; border-radius: 8px; white-space: pre-wrap; line-height: 1.45; font-size: 14px; }
    .user { align-self: flex-end; background: linear-gradient(135deg, #0a4f8f, #149ee7); color: white; }
    .assistant { align-self: flex-start; background: #edf7fc; color: #101c2e; border: 1px solid #d7eaf5; }
    .meta { align-self: flex-start; color: #52616b; font-size: 12px; padding: 0 2px; }
    .trace { align-self: flex-start; max-width: 92%; font-size: 12px; color: #334149; }
    .trace a { color: #0a76ba; font-weight: 700; text-decoration: none; }
    .feedback-button { border: 1px solid #c8d9e4; background: white; color: #52616b; border-radius: 999px; padding: 4px 9px; margin-left: 10px; font-size: 12px; font-weight: 800; line-height: 1; vertical-align: middle; }
    .feedback-button:hover { border-color: #0b83cc; color: #0b83cc; }
    .feedback-button:disabled { background: #edf3f7; color: #7d8b94; cursor: default; }
    .trace summary { cursor: pointer; color: #0a76ba; font-weight: 700; }
    .trace pre { background: #182026; color: #f5f7f8; padding: 12px; border-radius: 6px; overflow: auto; max-height: 360px; }
    form { display: flex; border-top: 1px solid var(--line); }
    textarea { flex: 1; border: 0; resize: vertical; min-height: 58px; max-height: 180px; padding: 14px; font: inherit; outline: none; }
    button { border: 0; background: #0b83cc; color: white; padding: 0 22px; font-weight: 700; cursor: pointer; }
    button:disabled { background: #9ba8ae; cursor: wait; }
    aside { padding: 16px; }
    h2 { font-size: 15px; margin: 0 0 12px; }
    .tool { border-top: 1px solid #edf1f3; padding: 10px 0; }
    .tool:first-of-type { border-top: 0; }
    .tool strong { display: block; font-size: 13px; }
    .tool span { display: block; color: #52616b; font-size: 12px; line-height: 1.35; margin-top: 4px; }
    .suggestions { display: flex; flex-direction: column; gap: 14px; margin-bottom: 18px; }
    .suggestion-group { border-bottom: 1px solid #edf1f3; padding-bottom: 12px; }
    .suggestion-group:last-child { border-bottom: 0; padding-bottom: 0; }
    .suggestion-category { color: #52616b; font-size: 11px; font-weight: 800; letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 8px; }
    .suggestion-button { display: block; width: 100%; border: 1px solid #d7eaf5; background: #f3f9fd; color: #101c2e; border-radius: 6px; padding: 9px 10px; margin: 6px 0; text-align: left; font-size: 12px; font-weight: 700; line-height: 1.35; cursor: pointer; }
    .suggestion-button:hover { border-color: #149ee7; background: #e7f5fd; }
    .small { color: #52616b; font-size: 12px; line-height: 1.45; }
    .nav { display: flex; gap: 10px; margin-top: 0; justify-content: flex-start; }
    .nav a { color: white; font-size: 15px; font-weight: 800; text-decoration: none; padding: 10px 15px; border: 1px solid rgba(47, 211, 255, 0.45); border-radius: 6px; background: rgba(20, 158, 231, 0.18); }
    .nav a:hover { background: rgba(47, 211, 255, 0.28); }
    .msg table, .data-table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
    .msg th, .msg td, .data-table th, .data-table td { border: 1px solid #ccd6da; padding: 7px 8px; text-align: left; vertical-align: top; }
    .msg th, .data-table th { background: #dde8e5; color: #182026; }
    .msg code { background: #dce7e4; padding: 1px 4px; border-radius: 4px; }
    .data-controls { display: flex; gap: 10px; align-items: center; margin: 16px 0; flex-wrap: wrap; }
    .session-list { display: flex; flex-direction: column; gap: 8px; }
    .session-list a { color: #0b4771; text-decoration: none; font-size: 13px; padding: 8px; border-radius: 6px; background: #edf7fc; }
    .session-list a.active { background: #0b4771; color: white; }
    .new-chat { display: block; text-align: center; background: #0b83cc; color: white; text-decoration: none; padding: 9px 10px; border-radius: 6px; font-weight: 700; margin-bottom: 12px; }
    select, input { border: 1px solid #c9d3d7; border-radius: 6px; padding: 8px 10px; font: inherit; background: white; }
    .table-wrap { overflow: auto; max-height: 68vh; border: 1px solid #d9e0e4; border-radius: 8px; background: white; }
    @media (max-width: 1040px) { .topbar-inner { grid-template-columns: 1fr; gap: 12px; } main { padding: 16px; } .shell { grid-template-columns: 1fr; } #messages { height: 58vh; } header { display: block; } .status { text-align: left; } .nav { flex-wrap: wrap; } }
  </style>
</head>
<body>
<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">Cyber<span>|</span>Gate</div>
    <nav class="nav"><a href="/">Chat</a><a href="/data">Data</a></nav>
    <div class="hero-title">Local CRM <span>Intelligence</span> Console</div>
  </div>
</div>
<main>
  <header>
    <div>
      <h1>{{ title }}</h1>
      <div class="small">Ollama model: {{ model }} | Source: {{ data_source }}</div>
    </div>
    <div class="status" id="status">Local CRM ready</div>
  </header>
  <div class="shell">
    <aside class="panel">
      <a class="new-chat" href="/new-chat">New chat</a>
      <h2>Chats</h2>
      <div id="sessions" class="session-list small">Loading...</div>
    </aside>
    <section class="panel">
      <div id="messages">
        <div class="msg assistant">{{ intro }}</div>
      </div>
      <form id="chatForm">
        <textarea id="prompt" placeholder="{{ placeholder }}" required></textarea>
        <button id="send" type="submit">Send</button>
      </form>
    </section>
    <aside class="panel">
      <h2>Suggested tests</h2>
      <div class="suggestions">
        <div class="suggestion-group">
          <div class="suggestion-category">Agent blocks</div>
          <button class="suggestion-button" type="button" data-prompt="What is the capital of France?">What is the capital of France?</button>
          <button class="suggestion-button" type="button" data-prompt="Who is the president of the United States?">Who is the president of the United States?</button>
          <button class="suggestion-button" type="button" data-prompt="What was the last match in the Premier League?">What was the last match in the Premier League?</button>
        </div>
        <div class="suggestion-group">
          <div class="suggestion-category">Single tool calling</div>
          <button class="suggestion-button" type="button" data-prompt="How many leads do we have?">How many leads do we have?</button>
          <button class="suggestion-button" type="button" data-prompt="What is our total sales revenue?">What is our total sales revenue?</button>
          <button class="suggestion-button" type="button" data-prompt="Show me overdue payments">Show me overdue payments</button>
        </div>
        <div class="suggestion-group">
          <div class="suggestion-category">Multi tool</div>
          <button class="suggestion-button" type="button" data-prompt="Analyze our sales pipeline and identify the weakest stage">Analyze our sales pipeline and identify the weakest stage</button>
          <button class="suggestion-button" type="button" data-prompt="Compare leads by industry and tell me where we should focus">Compare leads by industry and tell me where we should focus</button>
          <button class="suggestion-button" type="button" data-prompt="Identify our top performing accounts and their revenue contribution">Identify our top performing accounts and their revenue contribution</button>
        </div>
      </div>
    </aside>
  </div>
</main>
<script>
const messages = document.getElementById('messages');
const form = document.getElementById('chatForm');
const promptBox = document.getElementById('prompt');
const send = document.getElementById('send');
const statusEl = document.getElementById('status');
const sessionsEl = document.getElementById('sessions');
const chatId = new URLSearchParams(window.location.search).get('chat') || '{{ chat_id }}';

document.querySelectorAll('.suggestion-button').forEach((button) => {
  button.addEventListener('click', () => {
    promptBox.value = button.dataset.prompt || '';
    promptBox.focus();
  });
});

function addMessage(text, cls) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (cls === 'assistant') {
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderMarkdown(text) {
  const lines = String(text).trim().split(/\\r?\\n/);
  let html = '';
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('|') && lines[i + 1] && /^\\s*\\|?\\s*:?-{3,}:?/.test(lines[i + 1])) {
      const headers = line.split('|').slice(1, -1).map(c => c.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(lines[i].split('|').slice(1, -1).map(c => c.trim()));
        i++;
      }
      i--;
      html += '<table><thead><tr>' + headers.map(h => `<th>${escapeHtml(h)}</th>`).join('') + '</tr></thead><tbody>';
      html += rows.map(row => '<tr>' + row.map(c => `<td>${escapeHtml(c)}</td>`).join('') + '</tr>').join('');
      html += '</tbody></table>';
    } else if (line.trim()) {
      let rendered = escapeHtml(line)
        .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
      html += `<p>${rendered}</p>`;
    }
  }
  return html || escapeHtml(text);
}

function addMeta(text) {
  const div = document.createElement('div');
  div.className = 'meta';
  div.textContent = text;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function addTrace(traceId, disliked = false) {
  if (!traceId) return;
  const box = document.createElement('div');
  box.className = 'trace';
  box.innerHTML = `<a href="/trace-page/${traceId}" target="_blank" rel="noopener">Response details</a><button class="feedback-button" type="button" data-trace="${traceId}" ${disliked ? 'disabled' : ''}>${disliked ? 'Disliked' : 'Dislike'}</button>`;
  messages.appendChild(box);
  messages.scrollTop = messages.scrollHeight;
}

function renderTurn(turn) {
  addMessage(turn.user_message, 'user');
  if (turn.tool_name) addMeta(`Tool used: ${turn.tool_name}`);
  addMessage(turn.answer, 'assistant');
  addTrace(turn.trace_id, Boolean(turn.disliked));
}

async function loadTools() {
  const res = await fetch('/tools');
  const data = await res.json();
  if (!data.ok) {
    statusEl.textContent = data.error;
    return;
  }
  statusEl.textContent = `${data.tools.length} tools loaded`;
}

async function loadSessions() {
  const res = await fetch('/sessions');
  const data = await res.json();
  if (!data.ok) {
    sessionsEl.textContent = data.error;
    return;
  }
  sessionsEl.innerHTML = data.sessions.map(s => {
    const active = s.id === chatId ? ' active' : '';
    return `<a class="${active}" href="/?chat=${encodeURIComponent(s.id)}">${escapeHtml(s.title || 'Untitled chat')}<br><span>${escapeHtml(s.updated_at || '')}</span></a>`;
  }).join('');
}

async function loadHistory() {
  const res = await fetch(`/history?chat=${encodeURIComponent(chatId)}`);
  const data = await res.json();
  if (!data.ok || !data.turns.length) return;
  messages.innerHTML = '';
  data.turns.forEach(renderTurn);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = promptBox.value.trim();
  if (!text) return;
  promptBox.value = '';
  send.disabled = true;
  addMessage(text, 'user');
  addMeta('Thinking...');
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, chat_id: chatId})
    });
    const data = await res.json();
    messages.lastChild.remove();
    if (!data.ok) {
      addMessage(data.error, 'assistant');
    } else {
      if (data.tool_name) addMeta(`Tool used: ${data.tool_name}`);
      addMessage(data.answer, 'assistant');
      addTrace(data.trace_id, false);
      loadSessions();
    }
  } catch (err) {
    messages.lastChild.remove();
    addMessage(String(err), 'assistant');
  } finally {
    send.disabled = false;
    promptBox.focus();
  }
});

messages.addEventListener('click', async (event) => {
  const button = event.target.closest('.feedback-button');
  if (!button || button.disabled) return;
  const traceId = button.dataset.trace;
  button.disabled = true;
  button.textContent = 'Saving...';
  try {
    const res = await fetch('/feedback/dislike', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({trace_id: traceId})
    });
    const data = await res.json();
    button.textContent = data.ok ? 'Disliked' : 'Retry';
    button.disabled = Boolean(data.ok);
  } catch (err) {
    button.textContent = 'Retry';
    button.disabled = false;
  }
});

loadSessions();
loadHistory();
</script>
</body>
</html>
"""

DATA_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local CRM Data</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; --navy: #061427; --blue: #149ee7; --cyan: #2fd3ff; --ink: #101c2e; --line: #d7e5ee; }
    body { margin: 0; background: linear-gradient(180deg, #eef7fc 0%, #f7fbfe 46%, #edf4f8 100%); color: var(--ink); }
    .topbar { position: relative; overflow: hidden; background: radial-gradient(circle at 56% 0%, rgba(47, 211, 255, 0.22), transparent 34%), linear-gradient(135deg, #020915 0%, #061427 54%, #0a2742 100%); color: white; border-bottom: 4px solid var(--blue); }
    .topbar::after { content: ""; position: absolute; inset: 0; background: repeating-linear-gradient(135deg, transparent 0 18px, rgba(47, 211, 255, 0.72) 18px 26px, transparent 26px 44px); width: 190px; left: auto; opacity: 0.85; }
    .topbar-inner { position: relative; z-index: 1; max-width: 1320px; margin: 0 auto; padding: 16px 28px 20px; display: grid; grid-template-columns: 210px auto minmax(0, 1fr); gap: 24px; align-items: center; }
    .brand { font-weight: 900; font-size: 24px; line-height: 1; }
    .brand span, .hero-title span { color: var(--cyan); }
    .hero-title { font-size: 28px; font-weight: 800; line-height: 1.16; text-shadow: 0 2px 8px rgba(0, 0, 0, 0.36); }
    main { max-width: 1320px; margin: 0 auto; padding: 24px 28px 32px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; }
    h1 { font-size: 24px; margin: 0; }
    .small { color: #52616b; font-size: 12px; line-height: 1.45; }
    .nav { display: flex; gap: 10px; margin-top: 0; justify-content: flex-start; }
    .nav a { color: white; font-size: 15px; font-weight: 800; text-decoration: none; padding: 10px 15px; border: 1px solid rgba(47, 211, 255, 0.45); border-radius: 6px; background: rgba(20, 158, 231, 0.18); }
    .nav a:hover { background: rgba(47, 211, 255, 0.28); }
    .panel { background: rgba(255, 255, 255, 0.96); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 14px 36px rgba(7, 33, 54, 0.08); }
    .data-controls { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
    select, input, button { border: 1px solid #c9d3d7; border-radius: 6px; padding: 8px 10px; font: inherit; background: white; }
    button { background: #0b83cc; color: white; border-color: #0b83cc; cursor: pointer; font-weight: 700; }
    .table-wrap { overflow: auto; max-height: 68vh; border: 1px solid #d9e0e4; border-radius: 8px; background: white; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border-bottom: 1px solid #e5ecef; border-right: 1px solid #edf1f3; padding: 7px 8px; text-align: left; vertical-align: top; white-space: nowrap; }
    th { background: #dff1fb; position: sticky; top: 0; z-index: 1; }
    td { max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
    .status { margin: 10px 0; color: #52616b; font-size: 13px; }
    @media (max-width: 1040px) { .topbar-inner { grid-template-columns: 1fr; gap: 12px; } main { padding: 16px; } .nav { flex-wrap: wrap; } }
  </style>
</head>
<body>
<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">Cyber<span>|</span>Gate</div>
    <nav class="nav"><a href="/">Chat</a><a href="/data">Data</a></nav>
    <div class="hero-title">Local CRM <span>Data</span> Viewer</div>
  </div>
</div>
<main>
  <header>
    <div>
      <h1>Local CRM Data</h1>
      <div class="small">SQLite: {{ db_path }}</div>
    </div>
  </header>
  <section class="panel">
    <div class="data-controls">
      <label>Table <select id="tableSelect"></select></label>
      <label>Limit <input id="limitInput" type="number" min="1" max="200" value="50"></label>
      <label>Offset <input id="offsetInput" type="number" min="0" value="0"></label>
      <button id="loadButton">Load</button>
    </div>
    <div id="status" class="status">Loading...</div>
    <div class="table-wrap"><table id="dataTable"></table></div>
  </section>
</main>
<script>
const tableSelect = document.getElementById('tableSelect');
const limitInput = document.getElementById('limitInput');
const offsetInput = document.getElementById('offsetInput');
const loadButton = document.getElementById('loadButton');
const statusEl = document.getElementById('status');
const dataTable = document.getElementById('dataTable');

function escapeHtml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function init() {
  const res = await fetch('/data/tables');
  const data = await res.json();
  tableSelect.innerHTML = data.tables.map(t => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)} (${t.row_count})</option>`).join('');
  await loadData();
}

async function loadData() {
  const table = tableSelect.value;
  const limit = limitInput.value;
  const offset = offsetInput.value;
  statusEl.textContent = 'Loading...';
  const res = await fetch(`/data/rows?table=${encodeURIComponent(table)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`);
  const data = await res.json();
  if (!data.ok) {
    statusEl.textContent = data.error;
    dataTable.innerHTML = '';
    return;
  }
  statusEl.textContent = `${data.table}: showing ${data.rows.length} of ${data.total_rows} rows`;
  dataTable.innerHTML = '<thead><tr>' + data.columns.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr></thead>' +
    '<tbody>' + data.rows.map(row => '<tr>' + data.columns.map(c => `<td title="${escapeHtml(row[c])}">${escapeHtml(row[c])}</td>`).join('') + '</tr>').join('') + '</tbody>';
}

loadButton.addEventListener('click', loadData);
tableSelect.addEventListener('change', () => { offsetInput.value = 0; loadData(); });
init();
</script>
</body>
</html>
"""

TRACE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Response Trace</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
    body { margin: 0; background: #f5f7f8; color: #182026; }
    main { max-width: 1120px; margin: 0 auto; padding: 28px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; }
    h1 { font-size: 24px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 10px; }
    h3 { font-size: 14px; margin: 0 0 8px; color: #184d47; }
    .small { color: #52616b; font-size: 12px; line-height: 1.45; }
    .nav { display: flex; gap: 12px; margin-top: 8px; }
    .nav a { color: #184d47; font-size: 13px; font-weight: 700; text-decoration: none; }
    .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .metric, .step, .panel { background: #fff; border: 1px solid #d9e0e4; border-radius: 8px; padding: 14px; }
    .metric strong { display: block; font-size: 18px; margin-top: 4px; }
    .steps { display: grid; gap: 12px; }
    .step-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; border-bottom: 1px solid #edf1f3; padding-bottom: 8px; margin-bottom: 10px; }
    .badge { background: #dde8e5; color: #184d47; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 700; }
    pre { background: #182026; color: #f5f7f8; padding: 12px; border-radius: 6px; overflow: auto; max-height: 420px; white-space: pre-wrap; }
    code { background: #dce7e4; padding: 1px 4px; border-radius: 4px; }
    table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
    th, td { border: 1px solid #ccd6da; padding: 7px 8px; text-align: left; vertical-align: top; }
    th { background: #dde8e5; }
    .kv { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 8px; font-size: 13px; margin: 6px 0; }
    .error { border-color: #d7836b; background: #fff7f4; }
    @media (max-width: 860px) { main { padding: 16px; } .summary { grid-template-columns: 1fr 1fr; } .kv { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Response Trace</h1>
      <div class="small">Trace ID: {{ trace_id }}</div>
      <nav class="nav"><a href="/">Chat</a><a href="/data">Data</a></nav>
    </div>
  </header>

  <section class="summary">
    <div class="metric"><span class="small">Tool</span><strong>{{ trace.get("tool_name") or "none" }}</strong></div>
    <div class="metric"><span class="small">Steps</span><strong>{{ steps|length }}</strong></div>
    <div class="metric"><span class="small">Total</span><strong>{{ timings.get("total", "n/a") }} ms</strong></div>
    <div class="metric"><span class="small">Execution</span><strong>{{ timings.get("python_execution", timings.get("sql_execution", "n/a")) }} ms</strong></div>
  </section>

  <section class="panel">
    <h2>Question</h2>
    <p>{{ question }}</p>
  </section>

  <section class="steps" style="margin-top: 16px;">
    {% for step in steps %}
    <article class="step {% if step.get('error') %}error{% endif %}">
      <div class="step-head">
        <h3>{{ loop.index }}. {{ step.get("name", "step") }}</h3>
        {% if step.get("duration_ms") is not none %}<span class="badge">{{ step.get("duration_ms") }} ms</span>{% endif %}
      </div>
      {% if step.get("tool") %}<div class="kv"><strong>Tool</strong><span>{{ step.get("tool") }}</span></div>{% endif %}
      {% if step.get("reason") %}<div class="kv"><strong>Reason</strong><span>{{ step.get("reason") }}</span></div>{% endif %}
      {% if step.get("source") %}<div class="kv"><strong>Source</strong><span>{{ step.get("source") }}</span></div>{% endif %}
      {% if step.get("error") %}<h3>Error</h3><pre>{{ step.get("error") }}</pre>{% endif %}
      {% if step.get("input") %}<h3>Input</h3>{{ render_json(step.get("input"))|safe }}{% endif %}
      {% if step.get("output") %}<h3>Output</h3>{{ render_json(step.get("output"))|safe }}{% endif %}
    </article>
    {% endfor %}
  </section>

  <section class="panel" style="margin-top: 16px;">
    <h2>Raw Trace</h2>
    <pre>{{ raw_trace }}</pre>
  </section>
</main>
</body>
</html>
"""

FEEDBACK_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Disliked Responses</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; --navy: #061427; --blue: #149ee7; --cyan: #2fd3ff; --ink: #101c2e; --line: #d7e5ee; }
    body { margin: 0; background: #f3f8fb; color: var(--ink); }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; }
    h1 { font-size: 24px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 10px; }
    .small { color: #52616b; font-size: 12px; line-height: 1.45; }
    .nav { display: flex; gap: 12px; margin-top: 8px; }
    .nav a { color: #0a76ba; font-size: 13px; font-weight: 800; text-decoration: none; }
    .items { display: grid; gap: 14px; }
    .item { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 10px 26px rgba(7, 33, 54, 0.06); }
    .grid { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 8px; font-size: 13px; margin: 8px 0; }
    pre { background: #061427; color: #f5fbff; padding: 12px; border-radius: 6px; overflow: auto; max-height: 360px; white-space: pre-wrap; }
    table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
    th, td { border: 1px solid #ccd6da; padding: 7px 8px; text-align: left; vertical-align: top; }
    th { background: #dff1fb; }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Disliked Responses</h1>
      <div class="small">Hidden model diagnostics for responses users marked as not good enough.</div>
      <nav class="nav"><a href="/">Chat</a><a href="/data">Data</a></nav>
    </div>
  </header>
  <section class="items">
    {% if not items %}
      <article class="item"><p>No disliked responses yet.</p></article>
    {% endif %}
    {% for item in items %}
      <article class="item">
        <h2>{{ item.user_message }}</h2>
        <div class="grid"><strong>Disliked at</strong><span>{{ item.feedback_created_at }}</span></div>
        <div class="grid"><strong>Tool</strong><span>{{ item.tool_name or "none" }}</span></div>
        <div class="grid"><strong>Trace</strong><span><a href="/trace-page/{{ item.id }}" target="_blank" rel="noopener">{{ item.id }}</a></span></div>
        <h2>Answer</h2>
        <pre>{{ item.answer }}</pre>
        <h2>Hidden Diagnostic</h2>
        {{ render_json(item.feedback)|safe }}
      </article>
    {% endfor %}
  </section>
</main>
</body>
</html>
"""


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


def ollama_chat(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 900,
        },
    }
    if json_mode:
        payload["format"] = "json"
    response = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
    response.raise_for_status()
    return strip_thinking(response.json()["message"]["content"])


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
    def choose_local_sql(user_message: str, schema: list[dict[str, Any]], previous_error: str | None = None) -> str:
        text = user_message.lower()
    
    # Deterministic overrides
    if any(w in text for w in ['overdue', 'overdue payment', 'overdue invoice']):
        return "SELECT Subject, Account_Name, Grand_Total, Due_Date, Invoice_Status FROM Invoices WHERE Due_Date < date('now') ORDER BY Due_Date ASC LIMIT 50"

    table_names = {table["name"].lower(): table["name"] for table in schema}
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

    if any(token in text for token in ["risk", "risky", "sales operation", "focus next week", "workload"]):
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

    if "lead" in text and "industry" in text and (
        "annual_revenue" in text
        or "annual revenue" in text
        or "employee count" in text
        or "employees" in text
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
    return render_template_string(
        HTML,
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
    return render_template_string(DATA_HTML, db_path=str(LOCAL_DB_PATH))


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
    return render_template_string(
        TRACE_HTML,
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
    return render_template_string(
        FEEDBACK_HTML,
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
