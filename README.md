# Zoho Analytics MCP + Ollama Demo

This folder contains a small Flask app that connects:

- Zoho Analytics MCP Server through Docker
- Ollama through `qwen3.5:9b`
- A simple browser chat UI at `http://127.0.0.1:5000`

## Setup

1. Make sure Ollama is running and the model exists:

   ```powershell
   ollama list
   ```

2. Add Analytics MCP values to `.env`.

   Required:

   ```text
   ANALYTICS_CLIENT_ID
   ANALYTICS_CLIENT_SECRET
   ANALYTICS_REFRESH_TOKEN
   ANALYTICS_ORG_ID
   ```

   The app can reuse your existing `CLIENT_ID`, `CLIENT_SECRET`, and `REFRESH_TOKEN`, but only if those tokens were generated with Zoho Analytics API scopes. `ANALYTICS_ORG_ID` is always required.

3. Run the app:

   ```powershell
   python app.py
   ```

4. Open:

   ```text
   http://127.0.0.1:5000
   ```

## Useful Files

- `app.py` - Flask UI, Ollama chat, and Zoho Analytics MCP client.
- `export_zoho_schema.py` - CRM schema exporter.
- `generate_zoho_models.py` - Generates SQLAlchemy model classes from CRM field metadata.
- `seed_zoho_sqlite.py` - Creates and fills a local SQLite database with fake CRM-like data.
- `build_local_crm.py` - Runs schema export, model generation, and SQLite seeding together.
- `zoho_coql.py` - simple CRM COQL request script.

## Local CRM Mirror

Zoho CRM COQL uses module API names as table names and field API names as columns. The local mirror follows the same naming: each CRM module becomes a SQLite table, and each field API name becomes a column.

Run the whole pipeline:

```powershell
python build_local_crm.py --version v8 --rows 200 --sample-rows 5
```

This creates:

```text
zoho_schema.json
zoho_samples.json
zoho_crm_models.py
zoho_crm_local.sqlite3
```

The pipeline first exports CRM module/field metadata, then samples a few real records per module using COQL, then generates synthetic SQLite records. The seeder uses sampled values as hints where possible and fills the rest with generated values.

If you already exported `zoho_schema.json`, skip the Zoho API call:

```powershell
python build_local_crm.py --skip-export --rows 200
```

If you have a simple local text schema file like `schema`, skip Zoho entirely:

```powershell
python build_local_crm.py --text-schema schema --rows 200
```

## Local Chat Analysis

When `zoho_crm_local.sqlite3` exists, `app.py` runs in local mode and chats against the SQLite CRM data instead of Zoho Analytics MCP.

```powershell
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

The local chat supports:

- SQL queries for simple questions.
- `python_analysis` for pandas or Polars analytics. Analytical prompts can trigger model-generated Python code even if you do not explicitly mention pandas or Polars.
- Multiple chat sessions with a **New chat** button and persistent per-chat history in `chat_history.sqlite3`.
- Clickable response details showing multi-step traces: route decision, generated SQL or Python code, inputs, outputs, repair attempts, and timings.
- A separate formatted trace page at `/trace-page/<trace_id>`.
- Unsupported-question handling: if the local CRM schema lacks the requested data, the app says what is missing instead of forcing an answer.
- Markdown rendering for assistant responses, including tables.
- A data browser at `/data` for viewing local SQLite tables.

Example prompts:

```text
How many leads do we have?
How many deals are in each stage? use pandas
Use polars to calculate total Amount by Stage for Deals
Analyze the average deal amount by stage
```

To run each step manually:

```powershell
python refresh_zoho_token.py --save-token
python export_zoho_schema.py --version v8 --output zoho_schema.json --force-refresh --save-token
python export_zoho_samples.py --version v8 --schema zoho_schema.json --output zoho_samples.json --rows 5 --save-token
python generate_zoho_models.py --schema zoho_schema.json --output zoho_crm_models.py
python seed_zoho_sqlite.py --schema zoho_schema.json --samples zoho_samples.json --models zoho_crm_models.py --database zoho_crm_local.sqlite3 --rows 200 --drop-existing
```

The CRM schema export requires these Zoho CRM scopes:

```text
ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ
```

Or the broader testing scope:

```text
ZohoCRM.settings.ALL
```

The COQL sample export also needs module read access, such as:

```text
ZohoCRM.modules.ALL
```

## Notes

The first MCP request can be slow because Docker may need to pull `zohoanalytics/mcp-server:latest`.

If Zoho returns a token scope error, regenerate the OAuth refresh token with Analytics scopes.
For broad local testing, use:

```text
ZohoAnalytics.fullaccess.all
```

For narrower read/modeling workflows, start with:

```text
ZohoAnalytics.metadata.read,ZohoAnalytics.data.read,ZohoAnalytics.modeling.create
```
