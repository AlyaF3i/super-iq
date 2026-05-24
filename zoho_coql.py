import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_QUERY = "select id, Last_Name from Leads where id is not null limit 1"


def load_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value

    return values


def save_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rendered = f'{key} = "{value}"'
    updated = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            lines[index] = rendered
            updated = True
            break

    if not updated:
        lines.append(rendered)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def request_json(url: str, *, method: str, headers: dict[str, str], body=None) -> tuple[int, dict]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **headers}

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8")
        try:
            payload = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            payload = {"raw_response": response_body}
        return exc.code, payload


def refresh_access_token(env: dict[str, str], *, env_file: Path | None = None, persist: bool = False) -> str:
    required = ["REFRESH_TOKEN", "CLIENT_ID", "CLIENT_SECRET"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing required .env value(s) for refresh: {', '.join(missing)}")

    accounts_domain = env.get("ACCOUNTS_DOMAIN", "https://accounts.zoho.com").rstrip("/")
    url = f"{accounts_domain}/oauth/v2/token"
    form = urllib.parse.urlencode(
        {
            "refresh_token": env["REFRESH_TOKEN"],
            "client_id": env["CLIENT_ID"],
            "client_secret": env["CLIENT_SECRET"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    request = urllib.request.Request(url, data=form, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8")
        raise RuntimeError(f"Token refresh failed ({exc.code}): {response_body}") from exc

    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token refresh did not return an access_token: {payload}")
    env["ACCESS_TOKEN"] = access_token
    if persist and env_file is not None:
        save_env_value(env_file, "ACCESS_TOKEN", access_token)
    return access_token


def run_coql(
    env: dict[str, str],
    query: str,
    version: str,
    refresh_on_auth_error: bool,
    *,
    env_file: Path | None = None,
    persist_token: bool = False,
) -> tuple[int, dict]:
    api_domain = env.get("API_DOMAIN", "https://www.zohoapis.com").rstrip("/")
    access_token = env.get("ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("Missing ACCESS_TOKEN in .env")

    url = f"{api_domain}/crm/{version}/coql"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    status, payload = request_json(url, method="POST", headers=headers, body={"select_query": query})

    if status in {401, 403} and refresh_on_auth_error:
        access_token = refresh_access_token(env, env_file=env_file, persist=persist_token)
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        status, payload = request_json(url, method="POST", headers=headers, body={"select_query": query})

    return status, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Zoho CRM COQL request.")
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"COQL query to send. Default: {DEFAULT_QUERY!r}",
    )
    parser.add_argument(
        "--version",
        default=os.getenv("ZOHO_CRM_VERSION", "v2"),
        help="Zoho CRM API version, for example v2 or v8. Default: v2",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file containing Zoho credentials. Default: .env",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not refresh the access token if the first request is unauthorized.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Refresh the access token before sending the COQL request.",
    )
    parser.add_argument(
        "--save-token",
        action="store_true",
        help="Write a refreshed ACCESS_TOKEN back to the env file.",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    env = {**load_env(env_file), **os.environ}

    try:
        if args.force_refresh:
            refresh_access_token(env, env_file=env_file, persist=args.save_token)
        status, payload = run_coql(
            env=env,
            query=args.query,
            version=args.version,
            refresh_on_auth_error=not args.no_refresh,
            env_file=env_file,
            persist_token=args.save_token,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if payload.get("code") == "OAUTH_SCOPE_MISMATCH":
        print(
            "\nHint: regenerate the Zoho OAuth grant with COQL read access, "
            "for example ZohoCRM.coql.READ plus the relevant module READ/ALL scope."
        )
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
