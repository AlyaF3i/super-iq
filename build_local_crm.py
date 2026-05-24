import argparse
import subprocess
import sys
from pathlib import Path


def run(args: list[str]) -> None:
    print(f"$ {' '.join(args)}")
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Step failed with exit code {exc.returncode}: {' '.join(args)}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate local CRM models and seed SQLite from a text schema.")
    parser.add_argument("--text-schema", default="schema", help="Local text schema file. Default: schema")
    parser.add_argument("--schema", default="zoho_schema.json", help="Generated JSON schema path")
    parser.add_argument("--models", default="zoho_crm_models.py", help="Generated SQLAlchemy models path")
    parser.add_argument("--database", default="zoho_crm_local.sqlite3", help="Generated SQLite database path")
    parser.add_argument("--rows", type=int, default=200, help="Rows to generate per table. Default: 200")
    args = parser.parse_args()

    if not Path(args.text_schema).exists():
        raise SystemExit(f"Text schema file does not exist: {args.text_schema}")

    python = sys.executable
    run([python, "convert_text_schema.py", "--input", args.text_schema, "--output", args.schema])
    run([python, "generate_zoho_models.py", "--schema", args.schema, "--output", args.models])
    run(
        [
            python,
            "seed_zoho_sqlite.py",
            "--schema",
            args.schema,
            "--models",
            args.models,
            "--database",
            args.database,
            "--rows",
            str(args.rows),
            "--drop-existing",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
