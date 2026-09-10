"""Small administrative CLI."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from .stores import ApiTokenStore


async def create_api_token(name: str, description: str, expires_years: int = 0) -> None:
    store = ApiTokenStore(os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db"))
    expires_at = None
    if expires_years:
        now = datetime.now(timezone.utc)
        expires_at = now.replace(year=now.year + expires_years).timestamp()
    token, row = await store.create(name, description, expires_at)
    print("API token (shown once):")
    print(token)
    print("Token id:", row["id"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="tvproxy")
    sub = parser.add_subparsers(dest="command", required=True)
    token = sub.add_parser("create-api-token")
    token.add_argument("name")
    token.add_argument("--description", default="")
    token.add_argument("--expires-years", type=int, default=0)
    epg = sub.add_parser("refresh-epg")
    args = parser.parse_args()
    if args.command == "create-api-token":
        asyncio.run(create_api_token(args.name, args.description, args.expires_years))
    elif args.command == "refresh-epg":
        from .epg import EpgRefresher
        result = asyncio.run(EpgRefresher(os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")).refresh())
        print(result)


if __name__ == "__main__":
    main()
