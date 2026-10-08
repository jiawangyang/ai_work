"""Data export service: render query results as CSV or JSON file content."""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.models.schemas import QueryColumn


def _serialize_value(value: Any) -> Any:
    """
    Normalize a raw database value for export (shared by CSV and JSON).

    - None stays None (CSV renders it as an empty cell, JSON as null)
    - datetime/date become ISO 8601 strings
    - Decimal becomes a string to preserve precision
    - bytes are decoded as UTF-8 when possible, otherwise replaced by a placeholder
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(raw)} bytes>"
    return value


def _csv_cell(value: Any) -> Any:
    """Prepare a single value for a CSV cell."""
    serialized = _serialize_value(value)
    if serialized is None:
        return ""
    if serialized is True:
        return "true"
    if serialized is False:
        return "false"
    return serialized


def export_to_csv(columns: list[QueryColumn], rows: list[dict[str, Any]]) -> str:
    """
    Render a query result as CSV text.

    The output starts with a UTF-8 BOM (so Excel opens it without garbled
    non-ASCII characters), uses CRLF line endings, and the first row holds
    the column names in their original order.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([column.name for column in columns])
    for row in rows:
        writer.writerow([_csv_cell(row.get(column.name)) for column in columns])
    return "\ufeff" + buffer.getvalue()


def export_to_json(columns: list[QueryColumn], rows: list[dict[str, Any]]) -> str:
    """
    Render a query result as a pretty-printed JSON array of row objects.

    Values are normalized by ``_serialize_value``; any remaining exotic
    types fall back to their string representation via ``default=str``.
    """
    data = [
        {column.name: _serialize_value(row.get(column.name)) for column in columns}
        for row in rows
    ]
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
