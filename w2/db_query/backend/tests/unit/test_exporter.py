"""Unit tests for the data export service (CSV / JSON rendering)."""

import json
from datetime import date, datetime
from decimal import Decimal

from app.models.schemas import QueryColumn
from app.services.exporter import export_to_csv, export_to_json


def make_columns(*names: str) -> list[QueryColumn]:
    """Build a QueryColumn list from names."""
    return [QueryColumn(name=name, dataType="text") for name in names]


class TestExportToCsv:
    """CSV rendering rules."""

    def test_header_followed_by_rows_in_column_order(self):
        columns = make_columns("id", "name")
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

        text = export_to_csv(columns, rows)

        lines = text.split("\r\n")
        assert lines[0] == "\ufeffid,name"  # BOM glued to the header cell
        assert lines[1] == "1,Alice"
        assert lines[2] == "2,Bob"
        assert text.endswith("\r\n")

    def test_starts_with_utf8_bom(self):
        text = export_to_csv(make_columns("a"), [])
        assert text.startswith("\ufeff")

    def test_empty_result_contains_only_header(self):
        text = export_to_csv(make_columns("id", "name"), [])
        assert text.replace("\ufeff", "") == "id,name\r\n"

    def test_none_becomes_empty_string(self):
        rows = [{"id": 1, "name": None}]
        text = export_to_csv(make_columns("id", "name"), rows)
        assert "\r\n1,\r\n" in text

    def test_bool_rendered_as_true_false(self):
        rows = [{"id": 1, "active": True}, {"id": 2, "active": False}]
        text = export_to_csv(make_columns("id", "active"), rows)
        assert "1,true" in text
        assert "2,false" in text

    def test_datetime_rendered_as_isoformat(self):
        rows = [{"created_at": datetime(2026, 8, 18, 14, 30, 5)}]
        text = export_to_csv(make_columns("created_at"), rows)
        assert "2026-08-18T14:30:05" in text

    def test_date_rendered_as_isoformat(self):
        rows = [{"day": date(2026, 8, 18)}]
        text = export_to_csv(make_columns("day"), rows)
        assert "2026-08-18" in text

    def test_decimal_rendered_as_string_keeping_precision(self):
        rows = [{"amount": Decimal("12.50")}]
        text = export_to_csv(make_columns("amount"), rows)
        assert "12.50" in text

    def test_bytes_decoded_to_utf8(self):
        rows = [{"blob": b"hello"}]
        text = export_to_csv(make_columns("blob"), rows)
        assert "hello" in text

    def test_undecodable_bytes_become_placeholder(self):
        rows = [{"blob": b"\xff\xfe\x01"}]
        text = export_to_csv(make_columns("blob"), rows)
        assert "<binary 3 bytes>" in text

    def test_chinese_characters_kept_as_is(self):
        rows = [{"name": "张三"}]
        text = export_to_csv(make_columns("name"), rows)
        assert "张三" in text

    def test_special_characters_are_escaped(self):
        rows = [{"note": 'He said "hi", twice\nreally'}]
        text = export_to_csv(make_columns("note"), rows)
        assert '"He said ""hi"", twice\nreally"' in text

    def test_missing_column_renders_empty(self):
        rows = [{"id": 1}]
        text = export_to_csv(make_columns("id", "extra"), rows)
        assert "\r\n1,\r\n" in text


class TestExportToJson:
    """JSON rendering rules."""

    def test_renders_array_of_objects(self):
        columns = make_columns("id", "name")
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

        data = json.loads(export_to_json(columns, rows))

        assert data == [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]

    def test_none_becomes_null(self):
        rows = [{"id": 1, "name": None}]
        data = json.loads(export_to_json(make_columns("id", "name"), rows))
        assert data[0]["name"] is None

    def test_bool_stays_native(self):
        rows = [{"active": True}]
        data = json.loads(export_to_json(make_columns("active"), rows))
        assert data[0]["active"] is True

    def test_datetime_and_decimal_serialized(self):
        rows = [{"at": datetime(2026, 8, 18, 9, 0, 0), "amount": Decimal("0.10")}]
        data = json.loads(export_to_json(make_columns("at", "amount"), rows))
        assert data[0]["at"] == "2026-08-18T09:00:00"
        assert data[0]["amount"] == "0.10"

    def test_bytes_decoded_to_utf8(self):
        rows = [{"blob": b"hello"}]
        data = json.loads(export_to_json(make_columns("blob"), rows))
        assert data[0]["blob"] == "hello"

    def test_chinese_not_ascii_escaped(self):
        rows = [{"name": "张三"}]
        text = export_to_json(make_columns("name"), rows)
        assert "张三" in text
        assert "\\u" not in text

    def test_empty_rows_render_empty_array(self):
        assert export_to_json(make_columns("id"), []) == "[]"

    def test_missing_column_becomes_null(self):
        rows = [{"id": 1}]
        data = json.loads(export_to_json(make_columns("id", "extra"), rows))
        assert data[0]["extra"] is None
