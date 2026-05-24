# Local Zoho CRM Mock App

## 1. Install requirements

```powershell
pip install -r requirements.txt
```

## 2. Make sure Ollama is running

```powershell
ollama serve
```

The app expects this model:

```powershell
ollama pull qwen3.5:9b
```

## 3. Rebuild the local database

This reads `schema`, generates Python models, and creates `zoho_crm_local.sqlite3` with 200 synthetic rows per table.

```powershell
python build_local_crm.py --text-schema schema --rows 200
```

## 4. Start the app

```powershell
python app.py
```

## 5. Open the browser

Chat:

```text
http://127.0.0.1:5000
```

Data viewer:

```text
http://127.0.0.1:5000/data
```

## Notes

- The local database file is `zoho_crm_local.sqlite3`.
- Chat history is stored in `chat_history.sqlite3`.
- The app uses the local database by default when `zoho_crm_local.sqlite3` exists.
- Do not commit `.env`; it contains private keys.
