"""Database connection pool management.

This module provides utilities for creating and managing async connection
pools for PostgreSQL (asyncpg) and MySQL (aiomysql) databases.
"""

from typing import Any

import asyncpg
from asyncpg import Pool

from pg_mcp.config.settings import DatabaseConfig

# asyncpg.Pool and aiomysql.Pool share a duck-typed surface (acquire/close),
# so a minimal alias keeps signatures honest without a forced abstraction.
AnyPool = Any


async def _create_mysql_pool(config: DatabaseConfig) -> Pool:
    """Create an aiomysql connection pool (imported lazily)."""
    import aiomysql

    return await aiomysql.create_pool(
        host=config.host,
        port=config.port,
        db=config.name,
        user=config.user,
        password=config.password,
        minsize=config.min_pool_size,
        maxsize=config.max_pool_size,
        connect_timeout=int(config.pool_timeout),
        autocommit=True,
        pool_recycle=3600,
    )


async def create_pool(config: DatabaseConfig) -> Pool:
    """Create a connection pool for a single database.

    Dispatches to asyncpg or aiomysql based on ``config.db_type``.

    Args:
        config: Database configuration containing connection parameters
            and pool settings.

    Returns:
        Pool: An async connection pool instance (asyncpg or aiomysql).

    Raises:
        Exception: If connection to the database fails.

    Example:
        >>> config = DatabaseConfig(host="localhost", name="mydb")
        >>> pool = await create_pool(config)
        >>> async with pool.acquire() as conn:
        ...     result = await conn.fetch("SELECT 1")
    """
    if config.is_mysql:
        return await _create_mysql_pool(config)

    pool = await asyncpg.create_pool(
        host=config.host,
        port=config.port,
        database=config.name,
        user=config.user,
        password=config.password,
        min_size=config.min_pool_size,
        max_size=config.max_pool_size,
        timeout=config.pool_timeout,
        command_timeout=config.command_timeout,
    )

    if pool is None:
        raise RuntimeError(f"Failed to create connection pool for {config.name}")

    return pool


async def create_pools(configs: list[DatabaseConfig]) -> dict[str, AnyPool]:
    """Create connection pools for multiple databases.

    This function creates pools concurrently for all provided database
    configurations.

    Args:
        configs: List of database configurations.

    Returns:
        dict[str, Pool]: Dictionary mapping database names to their pools.

    Raises:
        asyncpg.PostgresError: If any database connection fails.

    Example:
        >>> configs = [
        ...     DatabaseConfig(name="db1", host="localhost"),
        ...     DatabaseConfig(name="db2", host="localhost"),
        ... ]
        >>> pools = await create_pools(configs)
        >>> assert "db1" in pools and "db2" in pools
    """
    pools: dict[str, Pool] = {}

    for config in configs:
        pool = await create_pool(config)
        pools[config.name] = pool

    return pools


async def close_pools(pools: dict[str, AnyPool], timeout: float = 10.0) -> None:  # noqa: ASYNC109
    """Close all connection pools gracefully.

    This function closes all pools and waits for all connections to be
    released properly. If graceful shutdown takes too long, it will
    forcefully terminate the pools (when the driver supports it).

    Args:
        pools: Dictionary mapping database names to their pools.
        timeout: Maximum time in seconds to wait for graceful shutdown
            before forcing termination. Default: 10.0 seconds.

    Example:
        >>> pools = await create_pools(configs)
        >>> # ... use pools ...
        >>> await close_pools(pools, timeout=5.0)
    """
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    for db_name, pool in pools.items():
        try:
            # Try graceful close with timeout.
            # asyncpg's close() returns a coroutine; aiomysql's is synchronous
            # (returns None) followed by an async wait_closed().
            close_result = pool.close()
            if close_result is not None:
                await asyncio.wait_for(close_result, timeout=timeout)
            # aiomysql pools expose wait_closed() after close()
            wait_closed = getattr(pool, "wait_closed", None)
            if wait_closed is not None:
                await asyncio.wait_for(wait_closed(), timeout=timeout)
            logger.info(f"Connection pool for '{db_name}' closed gracefully")
        except TimeoutError:
            # Force termination if graceful close times out
            logger.warning(f"Graceful close timed out for '{db_name}', forcing termination")
            terminate = getattr(pool, "terminate", None)
            if terminate is not None:
                terminate()
            logger.info(f"Connection pool for '{db_name}' terminated")
        except Exception as e:
            # Log error but continue closing other pools
            logger.error(f"Error closing pool for '{db_name}': {e!s}")
            # Force terminate on error
            terminate = getattr(pool, "terminate", None)
            if terminate is not None:
                terminate()
