"""Retry with exponential backoff for transient failures.

This module implements the retry/backoff behaviour described in the design
specification. It consumes :class:`ResilienceConfig` (``max_retries``,
``retry_delay``, ``backoff_factor``) so the previously unused configuration
fields now drive real behaviour.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pg_mcp.config.settings import ResilienceConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings that indicate a transient (retryable) upstream failure
TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "temporarily",
    "connection",
    "rate limit",
    "too many connections",
    "429",
    "502",
    "503",
    "504",
)


def is_transient_error(error: BaseException) -> bool:
    """Check whether an error is likely transient and therefore worth retrying.

    Args:
        error: The exception raised by the operation.

    Returns:
        bool: True when the error looks retryable (timeouts, connection issues,
            rate limiting, upstream 5xx responses).
    """
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True

    message = str(error).lower()
    return any(marker in message for marker in TRANSIENT_MARKERS)


def compute_backoff_delay(attempt: int, base_delay: float, backoff_factor: float) -> float:
    """Compute the delay before the next retry attempt.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        base_delay: Initial retry delay in seconds.
        backoff_factor: Exponential backoff multiplier.

    Returns:
        float: Delay in seconds (``base_delay * backoff_factor ** attempt``).
    """
    return base_delay * (backoff_factor**attempt)


async def _default_sleep(delay: float) -> None:
    """Default sleep coroutine used between attempts."""
    await asyncio.sleep(delay)


async def retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    *,
    config: ResilienceConfig | None = None,
    max_retries: int | None = None,
    base_delay: float | None = None,
    backoff_factor: float | None = None,
    should_retry: Callable[[BaseException], bool] = is_transient_error,
    operation_name: str = "operation",
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run an async operation, retrying transient failures with exponential backoff.

    Args:
        operation: Zero-argument coroutine function to execute.
        config: Resilience configuration providing defaults for retry settings.
        max_retries: Maximum number of retries (overrides ``config``).
        base_delay: Initial delay in seconds (overrides ``config``).
        backoff_factor: Backoff multiplier (overrides ``config``).
        should_retry: Predicate deciding whether an error is retryable.
        operation_name: Name used in log messages.
        sleep: Injectable sleep coroutine (used by tests).

    Returns:
        The value returned by ``operation``.

    Raises:
        Exception: The last error, either because it is not retryable or because
            all retry attempts have been exhausted.

    Example:
        >>> result = await retry_with_backoff(
        ...     lambda: generate_sql(question="Count users"),
        ...     config=ResilienceConfig(max_retries=3),
        ...     operation_name="generate_sql",
        ... )
    """
    resolved_max_retries = (
        max_retries if max_retries is not None else (config.max_retries if config else 0)
    )
    resolved_base_delay = (
        base_delay if base_delay is not None else (config.retry_delay if config else 1.0)
    )
    resolved_factor = (
        backoff_factor if backoff_factor is not None else (config.backoff_factor if config else 2.0)
    )
    sleeper = sleep if sleep is not None else _default_sleep

    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as error:
            if attempt >= resolved_max_retries or not should_retry(error):
                logger.warning(
                    "Operation '%s' failed and will not be retried",
                    operation_name,
                    extra={
                        "operation": operation_name,
                        "attempt": attempt + 1,
                        "retryable": should_retry(error),
                        "error": str(error),
                    },
                )
                raise

            delay = compute_backoff_delay(attempt, resolved_base_delay, resolved_factor)
            logger.warning(
                "Transient failure in '%s', retrying",
                operation_name,
                extra={
                    "operation": operation_name,
                    "attempt": attempt + 1,
                    "max_retries": resolved_max_retries,
                    "delay_seconds": delay,
                    "error": str(error),
                },
            )
            await sleeper(delay)
            attempt += 1
