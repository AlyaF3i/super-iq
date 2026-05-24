import argparse
import json
import sys
from pathlib import Path

from zoho_coql import load_env, refresh_access_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Zoho ACCESS_TOKEN using REFRESH_TOKEN.")
    parser.add_argument("--env-file", default=".env", help="Path to env file. Default: .env")
    parser.add_argument("--save-token", action="store_true", help="Write ACCESS_TOKEN back to the env file.")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    env = load_env(env_file)

    try:
        access_token = refresh_access_token(env, env_file=env_file, persist=args.save_token)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "access_token_refreshed": True, "saved": args.save_token}, indent=2))
    if not args.save_token:
        print("Run with --save-token to update ACCESS_TOKEN in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
