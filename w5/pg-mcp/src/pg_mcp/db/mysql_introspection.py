"""MySQL schema introspection.

This module provides functionality to introspect MySQL database schemas via
``information_schema``, extracting metadata about tables, views, columns,
foreign keys, indexes, and ENUM types. It mirrors the PostgreSQL
``SchemaIntrospector`` interface so callers can use them interchangeably.
"""

from typing import Any

import aiomysql
from aiomysql import Pool

from pg_mcp.models.schema import (
    ColumnInfo,
    DatabaseSchema,
    EnumTypeInfo,
    ForeignKeyInfo,
    IndexInfo,
    TableInfo,
)


def parse_enum_values(column_type: str) -> list[str]:
    """Parse ENUM values from a MySQL ``COLUMN_TYPE`` string.

    Handles quoted values containing commas and escaped quotes, e.g.
    ``enum('active','don''t know','a,b')``.

    Args:
        column_type: Raw COLUMN_TYPE string such as ``enum('a','b')``.

    Returns:
        list[str]: Parsed enum labels (empty if not an ENUM type).
    """
    if not column_type.lower().startswith("enum(") or not column_type.endswith(")"):
        return []

    inner = column_type[5:-1]
    values: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_quotes:
            if ch == "'" and i + 1 < len(inner) and inner[i + 1] == "'":
                buf.append("'")
                i += 1  # Skip escaped quote
            elif ch == "'":
                in_quotes = False
            else:
                buf.append(ch)
        elif ch == "'":
            in_quotes = True
        elif ch == ",":
            values.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf or values:
        values.append("".join(buf))
    return values


class MySQLSchemaIntrospector:
    """MySQL schema introspection service.

    Attributes:
        pool: aiomysql connection pool.
        database_name: Name of the database being introspected.
    """

    def __init__(self, pool: Pool, database_name: str):
        """Initialize the MySQL introspector.

        Args:
            pool: aiomysql connection pool.
            database_name: Name of the database to introspect.
        """
        self.pool = pool
        self.database_name = database_name

    async def _fetch_rows(self, conn: Any, query: str, *args: object) -> list[dict[str, Any]]:
        """Execute a query and return rows as plain dicts.

        Args:
            conn: Database connection.
            query: SQL query with ``%s`` placeholders.
            args: Query parameters.

        Returns:
            list[dict[str, Any]]: Result rows keyed by column name.
        """
        cursor = await conn.cursor(aiomysql.DictCursor)
        try:
            await cursor.execute(query, args)
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def _fetch_scalar(self, conn: Any, query: str, *args: object) -> Any:
        """Execute a query and return the first column of the first row.

        Args:
            conn: Database connection.
            query: SQL query with ``%s`` placeholders.
            args: Query parameters.

        Returns:
            Any: Scalar value or None when no rows are returned.
        """
        rows = await self._fetch_rows(conn, query, *args)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    async def introspect(self) -> DatabaseSchema:
        """Execute complete schema introspection.

        Returns:
            DatabaseSchema: Complete database schema information.
        """
        async with self.pool.acquire() as conn:
            version = await self._fetch_scalar(conn, "SELECT VERSION() AS v")

            tables = await self._get_tables(conn)
            views = await self._get_views(conn)
            enum_types = await self._get_enum_types(conn)

            for table in tables + views:
                table.columns = await self._get_columns(conn, table.table_name)
                table.foreign_keys = await self._get_foreign_keys(conn, table.table_name)
                table.indexes = await self._get_indexes(conn, table.table_name)

            return DatabaseSchema(
                database_name=self.database_name,
                db_type="mysql",
                tables=tables + views,
                enum_types=enum_types,
                version=version,
            )

    async def _get_tables(self, conn: Any) -> list[TableInfo]:
        """Get all base tables with comments and row estimates."""
        query = """
            SELECT
                TABLE_NAME AS table_name,
                TABLE_COMMENT AS comment,
                TABLE_ROWS AS row_estimate
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """
        rows = await self._fetch_rows(conn, query, self.database_name)
        return [
            TableInfo(
                schema_name=self.database_name,
                table_name=row["table_name"],
                comment=row["comment"] or None,
                row_count_estimate=int(row["row_estimate"])
                if row["row_estimate"] is not None
                else None,
            )
            for row in rows
        ]

    async def _get_views(self, conn: Any) -> list[TableInfo]:
        """Get all views."""
        query = """
            SELECT
                TABLE_NAME AS table_name,
                TABLE_COMMENT AS comment
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'VIEW'
            ORDER BY TABLE_NAME
        """
        rows = await self._fetch_rows(conn, query, self.database_name)
        return [
            TableInfo(
                schema_name=self.database_name,
                table_name=row["table_name"],
                comment=row["comment"] or None,
                row_count_estimate=None,
            )
            for row in rows
        ]

    async def _get_columns(self, conn: Any, table_name: str) -> list[ColumnInfo]:
        """Get column information for a table (PK/UNIQUE flags from COLUMN_KEY)."""
        query = """
            SELECT
                COLUMN_NAME AS column_name,
                COLUMN_TYPE AS column_type,
                DATA_TYPE AS data_type,
                IS_NULLABLE AS is_nullable,
                COLUMN_DEFAULT AS default_value,
                COLUMN_COMMENT AS comment,
                COLUMN_KEY AS column_key,
                EXTRA AS extra
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """
        rows = await self._fetch_rows(conn, query, self.database_name, table_name)

        columns: list[ColumnInfo] = []
        for row in rows:
            default_value: str | None = None
            if row["default_value"] is not None:
                default_value = str(row["default_value"])
            elif "auto_increment" in (row["extra"] or "").lower():
                default_value = "auto-increment"

            columns.append(
                ColumnInfo(
                    name=row["column_name"],
                    data_type=row["column_type"],
                    is_nullable=row["is_nullable"] == "YES",
                    default_value=default_value,
                    is_primary_key=row["column_key"] == "PRI",
                    is_unique=row["column_key"] == "UNI",
                    comment=row["comment"] or None,
                )
            )
        return columns

    async def _get_foreign_keys(self, conn: Any, table_name: str) -> list[ForeignKeyInfo]:
        """Get foreign key relationships for a table."""
        query = """
            SELECT
                CONSTRAINT_NAME AS constraint_name,
                COLUMN_NAME AS column_name,
                REFERENCED_TABLE_NAME AS referenced_table,
                REFERENCED_COLUMN_NAME AS referenced_column
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY CONSTRAINT_NAME
        """
        rows = await self._fetch_rows(conn, query, self.database_name, table_name)
        return [
            ForeignKeyInfo(
                constraint_name=row["constraint_name"],
                column_name=row["column_name"],
                referenced_table=row["referenced_table"],
                referenced_column=row["referenced_column"],
            )
            for row in rows
        ]

    async def _get_indexes(self, conn: Any, table_name: str) -> list[IndexInfo]:
        """Get index information for a table (excluding the PRIMARY index)."""
        query = """
            SELECT
                INDEX_NAME AS index_name,
                NON_UNIQUE AS non_unique,
                INDEX_TYPE AS index_type,
                GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND INDEX_NAME <> 'PRIMARY'
            GROUP BY INDEX_NAME, NON_UNIQUE, INDEX_TYPE
            ORDER BY INDEX_NAME
        """
        rows = await self._fetch_rows(conn, query, self.database_name, table_name)
        return [
            IndexInfo(
                name=row["index_name"],
                columns=row["columns"].split(",") if row["columns"] else [],
                is_unique=not row["non_unique"],
                index_type=row["index_type"],
            )
            for row in rows
        ]

    async def _get_enum_types(self, conn: Any) -> list[EnumTypeInfo]:
        """Collect ENUM column definitions as enum types.

        MySQL stores ENUMs per-column (not as standalone types), so each
        ENUM column becomes ``<table>.<column>`` type entry.
        """
        query = """
            SELECT
                TABLE_NAME AS table_name,
                COLUMN_NAME AS column_name,
                COLUMN_TYPE AS column_type
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND DATA_TYPE = 'enum'
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
        rows = await self._fetch_rows(conn, query, self.database_name)
        return [
            EnumTypeInfo(
                schema_name=self.database_name,
                type_name=f"{row['table_name']}.{row['column_name']}",
                values=parse_enum_values(row["column_type"]),
            )
            for row in rows
        ]


def create_introspector(pool: Any, database_name: str, db_type: str) -> Any:
    """Create the appropriate introspector for a database engine.

    Args:
        pool: Connection pool (asyncpg or aiomysql).
        database_name: Name of the database to introspect.
        db_type: Engine type ("postgresql" or "mysql").

    Returns:
        SchemaIntrospector | MySQLSchemaIntrospector: Matching introspector.
    """
    from pg_mcp.db.introspection import SchemaIntrospector

    if db_type == "mysql":
        return MySQLSchemaIntrospector(pool, database_name)
    return SchemaIntrospector(pool, database_name)
