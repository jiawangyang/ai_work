"""Integration tests for the MySQL layer (mock-based, no real MySQL).

Covers pool creation dispatch (aiomysql parameters), MySQL schema
introspection via information_schema, ENUM parsing, and schema-cache
db_type handling using fake aiomysql objects.
"""

from unittest.mock import AsyncMock, patch

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import CacheConfig, DatabaseConfig
from pg_mcp.db.mysql_introspection import (
    MySQLSchemaIntrospector,
    create_introspector,
    parse_enum_values,
)
from pg_mcp.db.pool import create_pool


class TestEnumParsing:
    """Test MySQL ENUM COLUMN_TYPE parsing."""

    def test_simple_enum(self) -> None:
        assert parse_enum_values("enum('active','inactive')") == ["active", "inactive"]

    def test_enum_with_comma_in_value(self) -> None:
        assert parse_enum_values("enum('a,b','c')") == ["a,b", "c"]

    def test_enum_with_escaped_quote(self) -> None:
        assert parse_enum_values("enum('it''s','ok')") == ["it's", "ok"]

    def test_non_enum_returns_empty(self) -> None:
        assert parse_enum_values("varchar(255)") == []
        assert parse_enum_values("") == []


class FakeCursor:
    """Fake aiomysql DictCursor serving rows canned on its connection."""

    def __init__(self, conn: "FakeMySQLConnection") -> None:
        self.conn = conn
        self._rows: list[dict] = []
        self.closed = False
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, args: tuple = ()) -> None:
        self.executed.append((query, args))
        self._rows = self.conn._rows_for(query)

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def close(self) -> None:
        self.closed = True


class FakeMySQLConnection:
    """Fake aiomysql connection dispatching queries to canned result sets."""

    def __init__(self) -> None:
        self.canned: list[tuple[str, list[dict]]] = []

    def add(self, match: str, rows: list[dict]) -> "FakeMySQLConnection":
        self.canned.append((match, rows))
        return self

    async def cursor(self, cursor_class: object) -> FakeCursor:
        return FakeCursor(self)

    def _rows_for(self, query: str) -> list[dict]:
        q = " ".join(query.split())
        for match, rows in self.canned:
            if match in q:
                return rows
        return []


class TestCreatePoolDispatch:
    """Test pool creation dispatches to aiomysql for MySQL configs."""

    async def test_mysql_config_passes_aiomysql_parameters(self) -> None:
        config = DatabaseConfig(
            name="todo_db",
            host="mysql.example.com",
            port=3306,
            user="root",
            password="secret",
            min_pool_size=2,
            max_pool_size=8,
            pool_timeout=12.0,
            db_type="mysql",
        )

        fake_pool = object()
        with patch("aiomysql.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = fake_pool
            pool = await create_pool(config)

        assert pool is fake_pool
        kwargs = mock_create.await_args.kwargs
        assert kwargs["host"] == "mysql.example.com"
        assert kwargs["port"] == 3306
        assert kwargs["db"] == "todo_db"
        assert kwargs["user"] == "root"
        assert kwargs["password"] == "secret"
        assert kwargs["minsize"] == 2
        assert kwargs["maxsize"] == 8
        assert kwargs["connect_timeout"] == 12
        assert kwargs["autocommit"] is True

    async def test_postgres_config_still_uses_asyncpg(self) -> None:
        config = DatabaseConfig(name="blog_small", host="pg.example.com")

        with patch("pg_mcp.db.pool.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = object()
            await create_pool(config)

        kwargs = mock_create.await_args.kwargs
        assert kwargs["database"] == "blog_small"
        assert kwargs["host"] == "pg.example.com"


class FakePool:
    """Pool stand-in yielding a FakeMySQLConnection from acquire()."""

    def __init__(self, conn: FakeMySQLConnection) -> None:
        self.conn = conn

    def acquire(self) -> object:
        return _PoolContext(self.conn)


class _PoolContext:
    def __init__(self, conn: FakeMySQLConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeMySQLConnection:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestMySQLSchemaIntrospection:
    """Test MySQL introspection with a fake connection."""

    async def test_introspect_returns_mysql_schema(self) -> None:
        conn = FakeMySQLConnection()
        conn.add("SELECT VERSION()", [{"v": "5.7.44-log"}])
        conn.add(
            "TABLE_TYPE = 'BASE TABLE'",
            [{"table_name": "users", "comment": "Registered users", "row_estimate": 3}],
        )
        conn.add("TABLE_TYPE = 'VIEW'", [])
        conn.add(
            "DATA_TYPE = 'enum'",
            [
                {
                    "table_name": "users",
                    "column_name": "status",
                    "column_type": "enum('active','inactive','banned')",
                }
            ],
        )
        conn.add(
            "ORDER BY ORDINAL_POSITION",
            [
                {
                    "column_name": "id",
                    "column_type": "int(11)",
                    "data_type": "int",
                    "is_nullable": "NO",
                    "default_value": None,
                    "comment": "User ID",
                    "column_key": "PRI",
                    "extra": "auto_increment",
                },
                {
                    "column_name": "email",
                    "column_type": "varchar(255)",
                    "data_type": "varchar",
                    "is_nullable": "NO",
                    "default_value": None,
                    "comment": "Login email",
                    "column_key": "UNI",
                    "extra": "",
                },
                {
                    "column_name": "status",
                    "column_type": "enum('active','inactive','banned')",
                    "data_type": "enum",
                    "is_nullable": "NO",
                    "default_value": "active",
                    "comment": "Account status",
                    "column_key": "",
                    "extra": "",
                },
            ],
        )
        conn.add(
            "REFERENCED_TABLE_NAME IS NOT NULL",
            [
                {
                    "constraint_name": "fk_todos_user",
                    "column_name": "user_id",
                    "referenced_table": "users",
                    "referenced_column": "id",
                }
            ],
        )
        conn.add(
            "INDEX_NAME <> 'PRIMARY'",
            [
                {
                    "index_name": "idx_todos_user",
                    "non_unique": 1,
                    "index_type": "BTREE",
                    "columns": "user_id",
                }
            ],
        )

        pool = FakePool(conn)
        introspector = MySQLSchemaIntrospector(pool, "todo_db")

        schema = await introspector.introspect()

        assert schema.database_name == "todo_db"
        assert schema.db_type == "mysql"
        assert schema.version == "5.7.44-log"

        users = schema.get_table("users", "todo_db")
        assert users is not None
        assert users.comment == "Registered users"
        assert users.row_count_estimate == 3
        assert users.schema_name == "todo_db"

        assert [c.name for c in users.columns] == ["id", "email", "status"]
        id_col = users.columns[0]
        assert id_col.is_primary_key is True
        assert id_col.default_value == "auto-increment"
        assert id_col.is_nullable is False
        email_col = users.columns[1]
        assert email_col.is_unique is True
        status_col = users.columns[2]
        assert status_col.is_primary_key is False
        assert status_col.default_value == "active"

        assert len(users.foreign_keys) == 1
        assert users.foreign_keys[0].referenced_table == "users"

        assert len(users.indexes) == 1
        assert users.indexes[0].name == "idx_todos_user"
        assert users.indexes[0].is_unique is False
        assert users.indexes[0].columns == ["user_id"]

        assert len(schema.enum_types) == 1
        assert schema.enum_types[0].type_name == "users.status"
        assert schema.enum_types[0].values == ["active", "inactive", "banned"]

    async def test_prompt_context_uses_mysql_label(self) -> None:
        conn = FakeMySQLConnection()
        conn.add("SELECT VERSION()", [{"v": "5.7.44-log"}])
        conn.add("TABLE_TYPE = 'BASE TABLE'", [])
        conn.add("TABLE_TYPE = 'VIEW'", [])
        conn.add("DATA_TYPE = 'enum'", [])
        conn.add("ORDER BY ORDINAL_POSITION", [])

        schema = await MySQLSchemaIntrospector(FakePool(conn), "todo_db").introspect()

        assert "MySQL Version: 5.7.44-log" in schema.to_prompt_context()
        assert "PostgreSQL Version" not in schema.to_prompt_context()


class TestCreateIntrospectorFactory:
    """Test the introspector factory dispatch."""

    def test_mysql_db_type(self) -> None:
        introspector = create_introspector(object(), "todo_db", "mysql")
        assert isinstance(introspector, MySQLSchemaIntrospector)

    def test_postgres_db_type(self) -> None:
        from pg_mcp.db.introspection import SchemaIntrospector

        introspector = create_introspector(object(), "blog_small", "postgresql")
        assert isinstance(introspector, SchemaIntrospector)


class TestSchemaCacheMySQL:
    """Test schema cache with MySQL db_type."""

    async def test_load_with_mysql_db_type_uses_mysql_introspector(self) -> None:
        cache = SchemaCache(CacheConfig(enabled=True))
        conn = FakeMySQLConnection()
        conn.add("SELECT VERSION()", [{"v": "8.0.36"}])
        conn.add("TABLE_TYPE = 'BASE TABLE'", [])
        conn.add("TABLE_TYPE = 'VIEW'", [])
        conn.add("DATA_TYPE = 'enum'", [])
        conn.add("ORDER BY ORDINAL_POSITION", [])

        schema = await cache.load("todo_db", FakePool(conn), "mysql")

        assert cache.get("todo_db") is schema
        assert schema.db_type == "mysql"
        assert schema.version == "8.0.36"
