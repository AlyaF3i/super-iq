import argparse
import json
import os
import sys
import time
from pathlib import Path

from zoho_coql import load_env, run_coql


def quote_field(api_name: str) -> str:
    return f'"{api_name}"' if not api_name.replace("_", "").isalnum() else api_name


def module_fields(module: dict, limit: int) -> list[str]:
    names = []
    for field in module.get("fields", []):
        api_name = field.get("api_name")
        if not api_name:
            continue
        data_type = str(field.get("data_type") or "").lower()
        if data_type in {"subform", "fileupload", "imageupload"}:
            continue
        names.append(api_name)
        if len(names) >= limit:
            break
    return names or ["id"]


def export_samples(
    env: dict[str, str],
    schema: dict,
    *,
    version: str,
    rows: int,
    field_limit: int,
    delay: float,
    env_file: Path | None,
    persist_token: bool,
) -> dict:
    output = {
        "api_version": version,
        "rows_per_module": rows,
        "modules": {},
    }

    modules = [module for module in schema.get("modules", []) if module.get("api_name") and module.get("fields")]
    for index, module in enumerate(modules, start=1):
        api_name = module["api_name"]
        fields = module_fields(module, field_limit)
        query = f"select {', '.join(quote_field(field) for field in fields)} from {api_name} limit {rows}"
        print(f"[{index}/{len(modules)}] Sampling {api_name}")
        status, payload = run_coql(
            env,
            query,
            version,
            refresh_on_auth_error=True,
            env_file=env_file,
            persist_token=persist_token,
        )
        output["modules"][api_name] = {
            "query": query,
            "http_status": status,
            "records": payload.get("data", []) if isinstance(payload, dict) else [],
            "response": payload if not (200 <= status < 300) else None,
        }
        if delay > 0:
            time.sleep(delay)

    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Export small real CRM samples with COQL for synthetic-data seeding.")
    parser.add_argument("--schema", default="zoho_schema.json", help="Schema JSON from export_zoho_schema.py")
    parser.add_argument("--output", default="zoho_samples.json", help="Output sample JSON file")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--version", default=os.getenv("ZOHO_CRM_VERSION", "v8"))
    parser.add_argument("--rows", type=int, default=5, help="Sample rows per module. Default: 5")
    parser.add_argument("--field-limit", type=int, default=50, help="Max fields per COQL query. Default: 50")
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--save-token", action="store_true", help="Write refreshed ACCESS_TOKEN back to the env file.")
    args = parser.parse_args()

    schema_path = Path(args.schema)
    if not schema_path.exists():
        print(f"Error: schema file does not exist: {schema_path}", file=sys.stderr)
        return 1

    env_file = Path(args.env_file)
    env = {**load_env(env_file), **os.environ}
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    samples = export_samples(
        env,
        schema,
        version=args.version,
        rows=args.rows,
        field_limit=args.field_limit,
        delay=args.delay,
        env_file=env_file,
        persist_token=args.save_token,
    )

    output_path = Path(args.output)
    output_path.write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8")
    record_count = sum(len(module["records"]) for module in samples["modules"].values())
    failed_count = sum(1 for module in samples["modules"].values() if module["http_status"] >= 300)
    print(f"Wrote {output_path} with {record_count} records; failed modules: {failed_count}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
