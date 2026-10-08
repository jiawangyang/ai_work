#!/usr/bin/env python
"""Execute a query (SQL or natural language) and export the result as CSV/JSON.

Thin CLI wrapper around the backend export API. Designed for humans and for
AI agents (see .claude/commands/export.md).

Examples:
    # Export with explicit SQL
    python scripts/export_query.py --db todo --sql "SELECT * FROM users" --format csv

    # Natural language mode: generate SQL first, confirm, then export
    python scripts/export_query.py --db todo --nl "active users with order counts" --format json

    # Non-interactive (automation / agents)
    python scripts/export_query.py --db todo --nl "top 10 users" --format csv --yes

Environment:
    DB_QUERY_API   Base URL of the backend API (default: http://localhost:8000)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from datetime import datetime
from urllib.request import Request, urlopen

API_BASE = os.environ.get("DB_QUERY_API", "http://localhost:8000")
TIMEOUT_SECONDS = 120
_FILENAME_RE = re.compile(r'filename="?([^";]+)"?')


def post_json(path: str, payload: dict):
    """POST a JSON payload and return the urlopen response (context manager)."""
    request = Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=TIMEOUT_SECONDS)


def read_http_error(error: urllib.error.HTTPError) -> str:
    """Extract a readable message from an HTTP error response body."""
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        return str(body.get("detail", body))
    except Exception:
        return str(error)


def filename_from_disposition(disposition: str | None) -> str | None:
    """Parse the filename out of a Content-Disposition header."""
    if not disposition:
        return None
    match = _FILENAME_RE.search(disposition)
    return match.group(1) if match else None


def resolve_sql(args: argparse.Namespace) -> str | None:
    """Return SQL from --sql, or generate it from --nl via the NL2SQL API."""
    if args.sql:
        return args.sql

    try:
        with post_json(
            f"/api/v1/dbs/{args.db}/query/natural", {"prompt": args.nl}
        ) as response:
            generated = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(f"NL2SQL failed ({error.code}): {read_http_error(error)}", file=sys.stderr)
        return None
    except urllib.error.URLError as error:
        print(
            f"Cannot reach the API at {API_BASE} ({error.reason}). Is the backend running?",
            file=sys.stderr,
        )
        return None

    print(f"Generated SQL:\n  {generated['sql']}")
    print(f"Explanation:\n  {generated['explanation']}")

    if args.yes:
        return generated["sql"]

    answer = input("Export with this SQL? [y/N] ").strip().lower()
    return generated["sql"] if answer in ("y", "yes") else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute a query and export the result as CSV or JSON."
    )
    parser.add_argument("--db", required=True, help="Database connection name")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sql", help="SQL SELECT query to execute and export")
    source.add_argument(
        "--nl", help="Natural language description of the query (NL2SQL)"
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Export format (default: csv)",
    )
    parser.add_argument(
        "--out",
        help="Output file path (default: exports/<server-provided filename>)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the SQL confirmation prompt in --nl mode",
    )
    args = parser.parse_args()

    sql = resolve_sql(args)
    if sql is None:
        print("Aborted.", file=sys.stderr)
        return 1

    try:
        with post_json(
            f"/api/v1/dbs/{args.db}/export", {"sql": sql, "format": args.format}
        ) as response:
            body = response.read()
            headers = response.headers
    except urllib.error.HTTPError as error:
        print(f"Export failed ({error.code}): {read_http_error(error)}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(
            f"Cannot reach the API at {API_BASE} ({error.reason}). Is the backend running?",
            file=sys.stderr,
        )
        return 1

    default_name = (
        filename_from_disposition(headers.get("Content-Disposition"))
        or f"query_{args.db}_{datetime.now():%Y%m%d_%H%M%S}.{args.format}"
    )
    out_path = args.out or os.path.join("exports", default_name)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as file:
        file.write(body)

    row_count = headers.get("X-Row-Count", "?")
    elapsed_ms = headers.get("X-Execution-Time-Ms", "?")
    print(f"Exported {row_count} rows ({elapsed_ms} ms) -> {os.path.abspath(out_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
