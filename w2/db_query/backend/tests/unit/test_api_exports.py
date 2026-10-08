"""Unit tests for export API endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models.database import ConnectionStatus, DatabaseConnection
from app.models.query import QueryHistory, QuerySource
from app.models.schemas import QueryColumn, QueryResult
from app.services.sql_validator import SqlValidationError


@pytest.fixture
def test_session():
    """Create an in-memory SQLite session for testing."""
    from app.models.database import DatabaseConnection  # noqa: F401
    from app.models.metadata import DatabaseMetadata  # noqa: F401
    from app.models.query import QueryHistory  # noqa: F401

    engine = create_engine(
        "sqlite:///file:test_export_db?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    """Create TestClient with test database session."""

    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    """Create a sample database connection."""
    conn = DatabaseConnection(
        name="test_db",
        url="postgresql://user:pass@localhost/testdb",
        description="Test database",
        status=ConnectionStatus.ACTIVE,
        last_connected_at=datetime.now(UTC).replace(tzinfo=None),
    )
    test_session.add(conn)
    test_session.commit()
    test_session.refresh(conn)
    return conn


def make_query_result() -> QueryResult:
    """Build a standard two-row query result for export tests."""
    return QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="character varying"),
        ],
        rows=[
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ],
        rowCount=2,
        executionTimeMs=12,
        sql="SELECT id, name FROM users LIMIT 1000",
    )


def add_history(
    test_session,
    sql: str,
    success: bool,
    executed_at: datetime | None = None,
) -> None:
    """Insert a query history entry."""
    test_session.add(
        QueryHistory(
            database_name="test_db",
            sql_text=sql,
            executed_at=executed_at or datetime.now(UTC).replace(tzinfo=None),
            execution_time_ms=10 if success else None,
            row_count=2 if success else None,
            success=success,
            error_message=None if success else "boom",
            query_source=QuerySource.MANUAL,
        )
    )
    test_session.commit()


class TestExportQueryResult:
    """Test the export endpoint."""

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_csv_success(self, mock_execute, client, sample_connection):
        """Export with explicit SQL as CSV returns an attachment with BOM content."""
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT id, name FROM users", "format": "csv"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert disposition.endswith('.csv"')
        assert response.headers["x-row-count"] == "2"

        text = response.content.decode("utf-8")
        assert text.startswith("\ufeff")
        assert "id,name" in text
        assert "1,Alice" in text

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_json_success(self, mock_execute, client, sample_connection):
        """Export with explicit SQL as JSON returns parsable row objects."""
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT id, name FROM users", "format": "json"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert ".json" in response.headers["content-disposition"]

        data = response.json()
        assert data == [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_defaults_to_csv_format(self, mock_execute, client, sample_connection):
        """When format is omitted, CSV is used."""
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT id, name FROM users"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_without_sql_replays_latest_successful_query(
        self, mock_execute, client, sample_connection, test_session
    ):
        """Without sql, the latest successful history entry is replayed."""
        now = datetime.now(UTC).replace(tzinfo=None)
        add_history(test_session, "SELECT old FROM t", success=True, executed_at=now)
        # A newer but FAILED query must not be picked
        add_history(
            test_session,
            "SELECT broken FROM t",
            success=False,
            executed_at=now + timedelta(seconds=60),
        )
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"format": "csv"},
        )

        assert response.status_code == 200
        mock_execute.assert_called_once()
        call_args = mock_execute.call_args[0]
        assert call_args[1] == "test_db"  # database_name
        assert call_args[4] == "SELECT old FROM t"  # sql
        assert call_args[5] == QuerySource.MANUAL

    def test_export_without_sql_and_history_returns_404(self, client, sample_connection):
        """Without sql and without any successful history, return 404."""
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"format": "csv"},
        )

        assert response.status_code == 404
        assert "No successful query" in response.json()["detail"]

    def test_export_database_not_found(self, client):
        """Export against a non-existent database returns 404."""
        response = client.post(
            "/api/v1/dbs/nonexistent/export",
            json={"sql": "SELECT 1", "format": "csv"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_export_invalid_format_returns_422(self, client, sample_connection):
        """Unsupported format is rejected by validation."""
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT 1", "format": "xml"},
        )

        assert response.status_code == 422

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_sql_validation_error_returns_400(
        self, mock_execute, client, sample_connection
    ):
        """SQL validation failures surface as 400."""
        mock_execute.side_effect = SqlValidationError("Only SELECT statements are allowed")

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "INSERT INTO users VALUES (1)", "format": "csv"},
        )

        assert response.status_code == 400
        assert "Only SELECT" in response.json()["detail"]

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_execution_error_returns_500(self, mock_execute, client, sample_connection):
        """Execution failures surface as 500 with context."""
        mock_execute.side_effect = Exception("Table does not exist")

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT * FROM invalid_table", "format": "csv"},
        )

        assert response.status_code == 500
        assert "Query execution failed" in response.json()["detail"]

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_empty_result_exports_header_only(
        self, mock_execute, client, sample_connection
    ):
        """Empty results still produce a valid file (header only for CSV)."""
        mock_execute.return_value = QueryResult(
            columns=[QueryColumn(name="id", dataType="integer")],
            rows=[],
            rowCount=0,
            executionTimeMs=3,
            sql="SELECT id FROM users WHERE 1 = 0",
        )

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT id FROM users WHERE 1 = 0", "format": "csv"},
        )

        assert response.status_code == 200
        text = response.content.decode("utf-8")
        assert text.replace("\ufeff", "") == "id\r\n"

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_filename_sanitized(self, mock_execute, client, sample_connection):
        """Unsafe filename characters are stripped to prevent header injection."""
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={
                "sql": "SELECT id FROM users",
                "format": "csv",
                "filename": "../../evil name",
            },
        )

        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert "/" not in disposition
        assert "\\" not in disposition
        assert "evil_name.csv" in disposition

    @patch("app.api.v1.exports.execute_query_with_service")
    def test_export_custom_filename_used(self, mock_execute, client, sample_connection):
        """A clean custom filename is used as-is."""
        mock_execute.return_value = make_query_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={
                "sql": "SELECT id FROM users",
                "format": "json",
                "filename": "users_snapshot",
            },
        )

        assert response.status_code == 200
        assert 'filename="users_snapshot.json"' in response.headers["content-disposition"]
