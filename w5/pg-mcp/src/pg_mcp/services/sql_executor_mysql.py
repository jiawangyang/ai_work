"""SQL executor for MySQL queries.

Provides safe SQL execution on top of aiomysql with session timeout
configuration, row limiting, and MySQL-specific error mapping. Read-only
safety is enforced by :class:`~pg_mcp.services.sql_validator.SQLValidator`
(MySQL has no server-side read-only transaction); this executor focuses on
timeouts, row limits, and type serialization (inherited from SQLExecutor).
"""

import asyncio
import logging
from typing import Any

from aiomysql import Connection as MySQLConnection
from aiomysql import DictCursor
from aiomysql import Pool as MySQLPool
from pymysql.err import Error as MySQLError

from pg_mcp.config.settings import DatabaseConfig, SecurityConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ExecutionTimeoutError,
    PgMcpError,
    SecurityViolationError,
)
from pg_mcp.services.sql_executor import SQLExecutor

logger = logging.getLogger(__name__)

# MySQL error numbers
ERR_QUERY_TIMEOUT = 3024  # Query execution was interrupted (max_execution_time)
ERR_COMMAND_DENIED = 1142  # SELECT command denied to user
ERR_ACCESS_DENIED_DB = 1044  # Access denied for user to database
ERR_ACCESS_DENIED_TABLE = 1144  # Access denied for user to table


class MySQLExecutor(SQLExecutor):
    """SQL executor using aiomysql.

    Differences from the PostgreSQL executor:
    - No read-only transaction (safety enforced by the SQL validator)
    - Session timeout via ``SET SESSION max_execution_time`` (best effort,
      requires MySQL 5.7.8+ and only applies to SELECT statements)
    - ``Connection.close()`` releases pooled connections back to the pool
    """

    def __init__(
        self,
        pool: MySQLPool,
        security_config: SecurityConfig,
        db_config: DatabaseConfig,
    ) -> None:
        """Initialize the MySQL executor.

        Args:
            pool: aiomysql connection pool.
            security_config: Security configuration including timeouts and limits.
            db_config: Database configuration including connection parameters.
        """
        super().__init__(pool, security_config, db_config)

    async def execute(
        self,
        sql: str,
        timeout: float | None = None,  # noqa: ASYNC109
        max_rows: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL query with row limiting and timeout.

        Args:
            sql: SQL query to execute (should already be validated).
            timeout: Query timeout in seconds (uses config default if None).
            max_rows: Maximum rows to return (uses config default if None).

        Returns:
            tuple: (results, total_row_count) before limiting.

        Raises:
            ExecutionTimeoutError: If query execution exceeds timeout.
            SecurityViolationError: If the user lacks permission for the query.
            DatabaseError: If the database operation fails.
        """
        timeout = timeout or self.security_config.max_execution_time
        max_rows = max_rows or self.security_config.max_rows

        connection: MySQLConnection | None = None
        try:
            try:
                connection = await asyncio.wait_for(
                    self.pool.acquire(), timeout=self.db_config.pool_timeout
                )
            except TimeoutError as e:
                raise ExecutionTimeoutError(
                    message=(
                        f"Timed out acquiring a connection within "
                        f"{self.db_config.pool_timeout} seconds"
                    ),
                    details={"timeout_seconds": self.db_config.pool_timeout},
                ) from e

            if connection is None:
                raise ExecutionTimeoutError(
                    message="Connection pool returned no connection",
                    details={"database": self.db_config.name},
                )

            await self._set_session_params(connection, timeout)

            cursor = await connection.cursor(DictCursor)
            try:
                try:
                    await asyncio.wait_for(cursor.execute(sql), timeout=timeout)
                    rows = list(await cursor.fetchall())
                except TimeoutError as e:
                    raise ExecutionTimeoutError(
                        message=f"Query execution exceeded timeout of {timeout} seconds",
                        details={
                            "timeout_seconds": timeout,
                            "sql": sql[:200],
                        },
                    ) from e
            finally:
                await cursor.close()

            total_count = len(rows)
            limited = rows[:max_rows] if len(rows) > max_rows else rows
            results = self._serialize_results(limited)
            return results, total_count

        except ExecutionTimeoutError:
            # Timed-out connections are not reused: close without releasing
            if connection is not None:
                self._discard_connection(connection)
            raise
        except MySQLError as e:
            raise self._map_mysql_error(e, sql) from e
        except Exception as e:
            raise DatabaseError(
                message=f"Unexpected error during query execution: {e!s}",
                details={
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                },
            ) from e
        finally:
            # aiomysql: close() returns a pooled connection to the pool
            if connection is not None:
                connection.close()

    async def _set_session_params(self, conn: MySQLConnection, timeout: float) -> None:  # noqa: ASYNC109
        """Set best-effort session query timeout.

        ``max_execution_time`` requires MySQL 5.7.8+ and only applies to
        SELECT statements. Failures are logged and ignored so that older
        servers or non-SELECT statements do not break execution.

        Args:
            conn: MySQL connection to configure.
            timeout: Query timeout in seconds (converted to milliseconds).
        """
        timeout_ms = int(timeout * 1000)
        try:
            cursor = await conn.cursor()
            try:
                await cursor.execute(f"SET SESSION max_execution_time = {timeout_ms}")
            finally:
                await cursor.close()
        except MySQLError as e:
            logger.warning("Failed to set max_execution_time: %s", e)

    def _discard_connection(self, connection: MySQLConnection) -> None:
        """Force-close a connection that must not be reused.

        Args:
            connection: The connection to discard.
        """
        try:
            connection.force_close()
        except Exception:
            logger.debug("Failed to force-close timed-out connection")

    def _map_mysql_error(self, error: MySQLError, sql: str) -> PgMcpError:
        """Map a MySQL error to the appropriate pg_mcp error type.

        Args:
            error: The raised pymysql/aiomysql error.
            sql: The SQL being executed (truncated into error details).

        Returns:
            DatabaseError: Timeout, security, or generic database error.
        """
        errno = error.args[0] if error.args else None

        if errno == ERR_QUERY_TIMEOUT:
            return ExecutionTimeoutError(
                message="Query execution exceeded max_execution_time",
                details={
                    "mysql_errno": errno,
                    "error_message": str(error),
                    "sql": sql[:200],
                },
            )
        if errno in (ERR_COMMAND_DENIED, ERR_ACCESS_DENIED_DB, ERR_ACCESS_DENIED_TABLE):
            return SecurityViolationError(
                message="Insufficient database privileges for this query",
                details={
                    "mysql_errno": errno,
                    "error_message": str(error),
                },
            )
        return DatabaseError(
            message=f"Database query failed: {error!s}",
            details={
                "mysql_errno": errno,
                "error_message": str(error),
                "sql": sql[:200],
            },
        )
