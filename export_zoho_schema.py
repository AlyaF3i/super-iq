import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from zoho_coql import load_env, refresh_access_token, request_json


def zoho_get(
    env: dict[str, str],
    path: str,
    *,
    version: str,
    params: dict[str, str] | None = None,
    access_token: str,
) -> tuple[int, dict]:
    api_domain = env.get("API_DOMAIN", "https://www.zohoapis.com").rstrip("/")
    url = f"{api_domain}/crm/{version}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    return request_json(
        url,
        method="GET",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
    )


def get_access_token(env: dict[str, str], *, force_refresh: bool, env_file: Path | None, persist_token: bool) -> str:
    if force_refresh:
        return refresh_access_token(env, env_file=env_file, persist=persist_token)

    access_token = env.get("ACCESS_TOKEN")
    if not access_token:
        return refresh_access_token(env, env_file=env_file, persist=persist_token)
    return access_token


def get_with_optional_refresh(
    env: dict[str, str],
    path: str,
    *,
    version: str,
    params: dict[str, str] | None,
    access_token: str,
    refresh_on_auth_error: bool,
    env_file: Path | None,
    persist_token: bool,
) -> tuple[int, dict, str]:
    status, payload = zoho_get(env, path, version=version, params=params, access_token=access_token)
    if status in {401, 403} and refresh_on_auth_error:
        access_token = refresh_access_token(env, env_file=env_file, persist=persist_token)
        status, payload = zoho_get(env, path, version=version, params=params, access_token=access_token)
    return status, payload, access_token


def export_schema(
    env: dict[str, str],
    *,
    version: str,
    include_inactive: bool,
    force_refresh: bool,
    refresh_on_auth_error: bool,
    delay_seconds: float,
    env_file: Path | None = None,
    persist_token: bool = False,
) -> dict:
    access_token = get_access_token(env, force_refresh=force_refresh, env_file=env_file, persist_token=persist_token)

    modules_status, modules_payload, access_token = get_with_optional_refresh(
        env,
        "settings/modules",
        version=version,
        params=None,
        access_token=access_token,
        refresh_on_auth_error=refresh_on_auth_error,
        env_file=env_file,
        persist_token=persist_token,
    )
    if not 200 <= modules_status < 300:
        raise RuntimeError(f"Could not fetch modules ({modules_status}): {json.dumps(modules_payload)}")

    modules = modules_payload.get("modules", [])
    if not include_inactive:
        modules = [module for module in modules if module.get("status") == "visible"]

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "api_version": version,
        "api_domain": env.get("API_DOMAIN", "https://www.zohoapis.com").rstrip("/"),
        "module_count": len(modules),
        "modules": [],
    }

    for index, module in enumerate(modules, start=1):
        api_name = module.get("api_name")
        module_schema = {
            "api_name": api_name,
            "module_metadata": module,
            "module_detail": None,
            "module_detail_error": None,
            "fields": [],
            "field_count": 0,
            "fields_error": None,
        }

        if not api_name:
            module_schema["fields_error"] = {"message": "Module did not include api_name"}
            export["modules"].append(module_schema)
            continue

        print(f"[{index}/{len(modules)}] Fetching module metadata for {api_name}")
        detail_status, detail_payload, access_token = get_with_optional_refresh(
            env,
            f"settings/modules/{urllib.parse.quote(api_name)}",
            version=version,
            params=None,
            access_token=access_token,
            refresh_on_auth_error=refresh_on_auth_error,
            env_file=env_file,
            persist_token=persist_token,
        )

        if 200 <= detail_status < 300:
            module_schema["module_detail"] = detail_payload
        else:
            module_schema["module_detail_error"] = {
                "http_status": detail_status,
                "response": detail_payload,
            }

        print(f"[{index}/{len(modules)}] Fetching field metadata for {api_name}")
        fields_status, fields_payload, access_token = get_with_optional_refresh(
            env,
            "settings/fields",
            version=version,
            params={"module": api_name},
            access_token=access_token,
            refresh_on_auth_error=refresh_on_auth_error,
            env_file=env_file,
            persist_token=persist_token,
        )

        if 200 <= fields_status < 300:
            fields = fields_payload.get("fields", [])
            module_schema["fields"] = fields
            module_schema["field_count"] = len(fields)
        else:
            module_schema["fields_error"] = {
                "http_status": fields_status,
                "response": fields_payload,
            }

        export["modules"].append(module_schema)

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    export["field_count"] = sum(module["field_count"] for module in export["modules"])
    export["failed_module_count"] = sum(
        1 for module in export["modules"] if module["fields_error"] or module["module_detail_error"]
    )
    return export


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Zoho CRM module and field schemas to JSON.")
    parser.add_argument("--env-file", default=".env", help="Path to the env file. Default: .env")
    parser.add_argument("--output", default="zoho_schema.json", help="Output JSON file. Default: zoho_schema.json")
    parser.add_argument(
        "--version",
        default=os.getenv("ZOHO_CRM_VERSION", "v2"),
        help="Zoho CRM API version, for example v2 or v8. Default: v2",
    )
    parser.add_argument("--include-inactive", action="store_true", help="Include hidden/inactive modules too.")
    parser.add_argument("--force-refresh", action="store_true", help="Refresh the access token before exporting.")
    parser.add_argument("--save-token", action="store_true", help="Write refreshed ACCESS_TOKEN back to the env file.")
    parser.add_argument("--no-refresh", action="store_true", help="Do not refresh token after an auth error.")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Seconds to wait between module field requests. Default: 0.15",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    env = {**load_env(env_file), **os.environ}

    try:
        schema = export_schema(
            env,
            version=args.version,
            include_inactive=args.include_inactive,
            force_refresh=args.force_refresh,
            refresh_on_auth_error=not args.no_refresh,
            delay_seconds=args.delay,
            env_file=env_file,
            persist_token=args.save_token,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    output_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {output_path}")
    print(
        f"Modules: {schema['module_count']} | Fields: {schema['field_count']} | "
        f"Failed modules: {schema['failed_module_count']}"
    )
    if schema["failed_module_count"]:
        print("Some modules failed; check module_detail_error and fields_error entries in the JSON.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
