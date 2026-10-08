"""Integration tests for the database layer (mock-based, no real PostgreSQL).

Verifies pool lifecycle management (``create_pool``/``create_pools``/
``close_pools``) and schema introspection + caching using mocked asyncpg
objects, so these tests run deterministically in CI without external services.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import CacheConfig, DatabaseConfig
from pg_mcp.db.introspection import SchemaIntrospector
from pg_mcp.db.pool import close_pools, create_pool, create_pools


class FakePool:
    """Minimal asyncpg.Pool stand-in for lifecycle tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.terminated = False
        self._close_delay = 0.0

    async def close(self) -> None:
        if self._close_delay:
            import asyncio

            await asyncio.sleep(self._close_delay)
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True
        self.closed = True


class TestPoolLifecycle:
    """Test pool creation and shutdown behaviour."""

    async def test_create_pool_uses_config_parameters(self) -> None:
        config = DatabaseConfig(
            host="db.example.com",
            port=6543,
            name="sales_db",
            user="analyst",
            password="secret",
            min_pool_size=2,
            max_pool_size=10,
            pool_timeout=15.0,
            command_timeout=20.0,
        )

        with patch("pg_mcp.db.pool.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = FakePool("sales_db")
            pool = await create_pool(config)

        assert isinstance(pool, FakePool)
        kwargs = mock_create.await_args.kwargs
        assert kwargs["host"] == "db.example.com"
        assert kwargs["port"] == 6543
        assert kwargs["database"] == "sales_db"
        assert kwargs["user"] == "analyst"
        assert kwargs["password"] == "secret"
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 10
        assert kwargs["timeout"] == 15.0
        assert kwargs["command_timeout"] == 20.0

    async def test_create_pools_maps_database_names(self) -> None:
        configs = [
            DatabaseConfig(name="db1"),
            DatabaseConfig(name="db2"),
        ]

        with patch(
            "pg_mcp.db.pool.asyncpg.create_pool",
            new_callable=AsyncMock,
            side_effect=lambda **kwargs: FakePool(kwargs["database"]),
        ):
            pools = await create_pools(configs)

        assert set(pools) == {"db1", "db2"}
        assert isinstance(pools["db1"], FakePool)

    async def test_create_pools_propagates_connection_errors(self) -> None:
        configs = [DatabaseConfig(name="unreachable")]

        with (
            patch(
                "pg_mcp.db.pool.asyncpg.create_pool",
                new_callable=AsyncMock,
                side_effect=ConnectionError("could not connect"),
            ),
            pytest.raises(ConnectionError),
        ):
            await create_pools(configs)

    async def test_close_pools_graceful(self) -> None:
        pools = {"db1": FakePool("db1"), "db2": FakePool("db2")}

        await close_pools(pools, timeout=5.0)

        assert all(pool.closed for pool in pools.values())
        assert not any(pool.terminated for pool in pools.values())

    async def test_close_pools_terminates_on_timeout(self) -> None:
        slow_pool = FakePool("slow")
        slow_pool._close_delay = 1.0  # Longer than the close timeout
        pools = {"slow": slow_pool}

        await close_pools(pools, timeout=0.05)

        assert slow_pool.terminated is True

    async def test_close_pools_continues_on_error(self) -> None:
        broken_pool = FakePool("broken")
        broken_pool.close = AsyncMock(side_effect=RuntimeError("close failed"))
        good_pool = FakePool("good")
        pools = {"broken": broken_pool, "good": good_pool}

        await close_pools(pools, timeout=1.0)

        # The broken pool is force-terminated, the good one closed gracefully
        assert broken_pool.terminated is True
        assert good_pool.closed is True


class FakeRecord(dict):
    """dict subclass that supports record["key"] access like asyncpg.Record."""


class FakeConnection:
    """Fake asyncpg connection that serves canned introspection results."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def fetchval(self, query: str, *args: object) -> object:
        if "version()" in query:
            return "PostgreSQL 15.4 (Debian), 64-bit"
        if "reltuples" in query:
            return 123
        # Unique-constraint EXISTS checks and any other scalar lookups
        return False

    async def fetch(self, query: str, *args: object) -> list[FakeRecord]:
        q = " ".join(query.split())

        if "relkind = 'r'" in q:
            return [FakeRecord(schema_name="public", table_name="users", comment="User accounts")]
        if "relkind IN ('v','m')" in q or "relkind = 'v'" in q:
            return []
        if "pg_type" in q:
            return []
        if "relname AS index_name" in q:
            return [
                FakeRecord(
                    index_name="users_email_idx",
                    is_unique=False,
                    index_type="btree",
                    columns=["email"],
                )
            ]
        if "indisprimary" in q:
            return [FakeRecord(column_name="id")]
        if "contype = 'f'" in q:
            return []
        if "reltuples" in q:
            return [FakeRecord(estimate=123)]
        if "attname AS column_name" in q:
            return [
                FakeRecord(
                    column_name="id",
                    data_type="integer",
                    is_nullable=False,
                    default_value=None,
                    comment=None,
                ),
                FakeRecord(
                    column_name="email",
                    data_type="character varying(255)",
                    is_nullable=False,
                    default_value=None,
                    comment=None,
                ),
            ]
        return []

    async def fetchrow(self, query: str, *args: object) -> FakeRecord | None:
        q = " ".join(query.split())
        if " indisunique " in q or "pg_index" in q:
            return FakeRecord(is_unique=True)
        return None


class FakePoolWithConnection:
    """Pool stand-in that yields a FakeConnection from acquire()."""

    def __init__(self) -> None:
        self.connection = FakeConnection()

    def acquire(self) -> object:
        return _ConnectionContext(self.connection)


class _ConnectionContext:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestSchemaIntrospection:
    """Test schema introspection and caching with a mocked connection."""

    async def test_introspect_returns_populated_schema(self) -> None:
        pool = FakePoolWithConnection()
        introspector = SchemaIntrospector(pool, "sales_db")  # type: ignore[arg-type]

        schema = await introspector.introspect()

        assert schema.database_name == "sales_db"
        assert schema.version.startswith("PostgreSQL 15.4")
        table_names = [t.table_name for t in schema.tables]
        assert "users" in table_names

        users = next(t for t in schema.tables if t.table_name == "users")
        assert users.comment == "User accounts"
        column_names = [c.name for c in users.columns]
        assert column_names == ["id", "email"]
        assert users.columns[0].is_primary_key is True
        assert users.indexes  # At least the PK index was picked up
        assert users.row_count_estimate == 123

    async def test_schema_cache_load_and_get(self) -> None:
        cache = SchemaCache(CacheConfig(enabled=True))
        pool = FakePoolWithConnection()

        loaded = await cache.load("sales_db", pool)  # type: ignore[arg-type]

        assert cache.get("sales_db") is loaded
        assert cache.get_cached_databases() == ["sales_db"]

    async def test_schema_cache_disabled_returns_none(self) -> None:
        cache = SchemaCache(CacheConfig(enabled=False))
        pool = FakePoolWithConnection()

        loaded = await cache.load("sales_db", pool)  # type: ignore[arg-type]

        assert cache.get("sales_db") is None
        assert cache.get_cached_databases() == []
        # The schema is still returned to the caller even though not cached
        assert loaded.database_name == "sales_db"

    async def test_schema_cache_clear(self) -> None:
        cache = SchemaCache(CacheConfig(enabled=True))
        pool = FakePoolWithConnection()

        await cache.load("db1", pool)  # type: ignore[arg-type]
        await cache.load("db2", pool)  # type: ignore[arg-type]
        cache.clear("db1")

        assert cache.get("db1") is None
        assert cache.get("db2") is not None

        cache.clear()
        assert cache.get_cached_databases() == []


class TestExecutorSessionSetup:
    """Test SQL executor session parameter wiring against a mock connection."""

    async def test_execute_sets_session_params_and_readonly_transaction(self) -> None:
        from pg_mcp.config.settings import SecurityConfig
        from pg_mcp.services.sql_executor import SQLExecutor

        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[FakeRecord(id=1, name="alice")])
        conn.transaction = MagicMock(
            return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
        )

        pool = MagicMock()
        pool.acquire = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=conn),
                __aexit__=AsyncMock(),
            )
        )

        security = SecurityConfig(
            max_execution_time=5.0,
            max_rows=100,
            safe_search_path="public",
            readonly_role="readonly_user",
        )
        db_config = DatabaseConfig(name="testdb")
        executor = SQLExecutor(pool=pool, security_config=security, db_config=db_config)

        results, total = await executor.execute("SELECT id, name FROM users")

        assert total == 1
        assert results == [{"id": 1, "name": "alice"}]

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert any("statement_timeout = 5000" in stmt for stmt in executed)
        assert any("search_path = 'public'" in stmt for stmt in executed)
        assert any("SET ROLE readonly_user" in stmt for stmt in executed)
        # The read-only transaction was entered
        conn.transaction.assert_called_once_with(readonly=True)
