"""Unit tests for the MySQL executor (mocked aiomysql objects)."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pymysql.err import OperationalError

from pg_mcp.config.settings import DatabaseConfig, SecurityConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ExecutionTimeoutError,
    SecurityViolationError,
)
from pg_mcp.services.sql_executor_mysql import MySQLExecutor


class FakeCursor:
    """Fake aiomysql DictCursor."""

    def __init__(self, rows: list[dict], execute_error: Exception | None = None) -> None:
        self._rows = rows
        self._execute_error = execute_error
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, sql: str, args: tuple = ()) -> None:
        self.executed.append(sql)
        if self._execute_error is not None:
            raise self._execute_error

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Fake aiomysql connection."""

    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_value = cursor
        # Cursor used for session-param statements (created without a class arg)
        self.session_cursor = FakeCursor([])
        self.closed = False
        self.force_closed = False

    async def cursor(self, cursor_class: object | None = None) -> FakeCursor:
        if cursor_class is None:
            return self.session_cursor
        return self.cursor_value

    def close(self) -> None:
        self.closed = True

    def force_close(self) -> None:
        self.force_closed = True
        self.closed = True


class FakePool:
    """Fake aiomysql pool."""

    def __init__(self, conn: FakeConnection, acquire_delay: float = 0.0) -> None:
        self.conn = conn
        self.acquire_delay = acquire_delay

    async def acquire(self) -> FakeConnection:
        if self.acquire_delay:
            await asyncio.sleep(self.acquire_delay)
        return self.conn


def make_executor(pool: FakePool) -> MySQLExecutor:
    security = SecurityConfig(max_execution_time=5.0, max_rows=100)
    db_config = DatabaseConfig(name="todo_db", pool_timeout=2.0, db_type="mysql")
    return MySQLExecutor(pool=pool, security_config=security, db_config=db_config)  # type: ignore[arg-type]


class TestSuccessfulExecution:
    """Tests for the happy path."""

    async def test_returns_serialized_rows_and_total(self) -> None:
        cursor = FakeCursor([{"id": 1, "email": "a@x.com"}, {"id": 2, "email": "b@x.com"}])
        conn = FakeConnection(cursor)
        executor = make_executor(FakePool(conn))

        results, total = await executor.execute("SELECT id, email FROM users")

        assert total == 2
        assert results[0] == {"id": 1, "email": "a@x.com"}
        assert cursor.executed == ["SELECT id, email FROM users"]
        assert conn.closed is True  # Returned to the pool

    async def test_rows_limited_to_max_rows(self) -> None:
        rows = [{"id": i} for i in range(150)]
        cursor = FakeCursor(rows)
        executor = make_executor(FakePool(FakeConnection(cursor)))

        results, total = await executor.execute("SELECT id FROM users")

        assert total == 150
        assert len(results) == 100

    async def test_sets_session_max_execution_time(self) -> None:
        cursor = FakeCursor([])
        conn = FakeConnection(cursor)
        executor = make_executor(FakePool(conn))

        await executor.execute("SELECT 1")

        assert any(
            "SET SESSION max_execution_time = 5000" in stmt
            for stmt in conn.session_cursor.executed
        )


class TestTimeouts:
    """Tests for timeout handling."""

    async def test_query_timeout_discards_connection(self) -> None:
        import asyncio

        cursor = FakeCursor([])
        conn = FakeConnection(cursor)

        async def slow_execute(sql: str, args: tuple = ()) -> None:
            await asyncio.sleep(1.0)

        cursor.execute = slow_execute
        conn.cursor_value = cursor
        executor = make_executor(FakePool(conn))

        with pytest.raises(ExecutionTimeoutError, match="exceeded timeout"):
            await executor.execute("SELECT SLEEP(10)", timeout=0.05)

        assert conn.force_closed is True

    async def test_acquire_timeout_raises_execution_timeout(self) -> None:
        pool = FakePool(FakeConnection(FakeCursor([])), acquire_delay=2.0)
        executor = make_executor(pool)
        executor.db_config = DatabaseConfig(name="todo_db", pool_timeout=1.0, db_type="mysql")

        with pytest.raises(ExecutionTimeoutError, match="acquiring a connection"):
            await executor.execute("SELECT 1")


class TestErrorMapping:
    """Tests for MySQL errno mapping."""

    async def test_query_timeout_errno_maps_to_execution_timeout(self) -> None:
        error = OperationalError(3024, "Query execution was interrupted")
        cursor = FakeCursor([], execute_error=error)
        executor = make_executor(FakePool(FakeConnection(cursor)))

        with pytest.raises(ExecutionTimeoutError, match="max_execution_time"):
            await executor.execute("SELECT 1")

    async def test_command_denied_maps_to_security_violation(self) -> None:
        error = OperationalError(1142, "SELECT command denied to user")
        cursor = FakeCursor([], execute_error=error)
        executor = make_executor(FakePool(FakeConnection(cursor)))

        with pytest.raises(SecurityViolationError, match="privileges"):
            await executor.execute("SELECT * FROM secret_table")

    async def test_generic_error_maps_to_database_error(self) -> None:
        error = OperationalError(1146, "Table 'todo_db.missing' doesn't exist")
        cursor = FakeCursor([], execute_error=error)
        executor = make_executor(FakePool(FakeConnection(cursor)))

        with pytest.raises(DatabaseError, match="doesn't exist"):
            await executor.execute("SELECT * FROM missing")

    async def test_session_param_failure_does_not_block_execution(self) -> None:
        cursor = FakeCursor([{"id": 1}])
        conn = FakeConnection(cursor)
        conn.session_cursor.execute = AsyncMock(
            side_effect=OperationalError(1193, "Unknown system variable")
        )
        executor = make_executor(FakePool(conn))

        results, total = await executor.execute("SELECT id FROM users")

        assert total == 1
        assert results == [{"id": 1}]


class TestSerialization:
    """Tests for MySQL type serialization."""

    async def test_datetime_and_decimal_serialized(self) -> None:
        import datetime
        import decimal

        cursor = FakeCursor(
            [
                {
                    "created": datetime.datetime(2026, 1, 1, 12, 0),
                    "price": decimal.Decimal("9.99"),
                }
            ]
        )
        executor = make_executor(FakePool(FakeConnection(cursor)))

        results, _ = await executor.execute("SELECT created, price FROM orders")

        assert results[0]["created"] == "2026-01-01T12:00:00"
        assert results[0]["price"] == 9.99
