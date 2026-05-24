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
    parser = argparse.ArgumentParser(description="Export Zoho CRM schema, generate models, and seed SQLite.")
    parser.add_argument("--version", default="v8", help="Zoho CRM API version. Default: v8")
    parser.add_argument("--schema", default="zoho_schema.json")
    parser.add_argument("--models", default="zoho_crm_models.py")
    parser.add_argument("--database", default="zoho_crm_local.sqlite3")
    parser.add_argument("--samples", default="zoho_samples.json")
    parser.add_argument("--sample-rows", type=int, default=5)
    parser.add_argument("--rows", type=int, default=25)
    parser.add_argument("--skip-export", action="store_true", help="Use an existing schema JSON file")
    parser.add_argument("--skip-samples", action="store_true", help="Do not fetch real sample rows with COQL")
    parser.add_argument("--text-schema", default=None, help="Use a local text schema file instead of Zoho schema export")
    args = parser.parse_args()

    python = sys.executable
    if args.text_schema:
        run([python, "convert_text_schema.py", "--input", args.text_schema, "--output", args.schema])
        args.skip_samples = True
    elif not args.skip_export:
        run(
            [
                python,
                "export_zoho_schema.py",
                "--version",
                args.version,
                "--output",
                args.schema,
                "--force-refresh",
                "--save-token",
            ]
        )

    if not Path(args.schema).exists():
        raise SystemExit(f"Schema file does not exist: {args.schema}")

    if not args.skip_samples:
        run(
            [
                python,
                "export_zoho_samples.py",
                "--version",
                args.version,
                "--schema",
                args.schema,
                "--output",
                args.samples,
                "--rows",
                str(args.sample_rows),
                "--save-token",
            ]
        )

    run([python, "generate_zoho_models.py", "--schema", args.schema, "--output", args.models])
    seed_command = [
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
    if Path(args.samples).exists():
        seed_command.extend(["--samples", args.samples])
    run(seed_command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
