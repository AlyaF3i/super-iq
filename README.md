# Local Zoho CRM Mock App

This repo runs a local-only CRM analytics demo with:

- Flask chat UI
- Ollama model `qwen3.5:9b`
- SQLite synthetic CRM database
- pandas/Polars-style analysis through model-generated Python
- Data browser at `/data`

No Zoho account or Zoho API token is required for the local workflow.

## Start

```powershell
pip install -r requirements.txt
ollama pull qwen3.5:9b
python build_local_crm.py
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Data browser:

```text
http://127.0.0.1:5000/data
```

## Generated Local Files

These are generated locally and are intentionally ignored by git:

- `zoho_crm_local.sqlite3`
- `chat_history.sqlite3`
- `zoho_crm_models.py`
- `zoho_schema.json`

Regenerate them with:

```powershell
python build_local_crm.py
```

## Useful Files

- `app.py` - Flask UI, local chat, trace pages, and data browser.
- `schema` - local CRM table/field definition.
- `build_local_crm.py` - rebuilds the local schema/models/database.
- `convert_text_schema.py` - converts `schema` into generated JSON metadata.
- `generate_zoho_models.py` - generates SQLAlchemy models from local metadata.
- `seed_zoho_sqlite.py` - creates synthetic CRM records.
- `AHMED.md` - short startup steps.
