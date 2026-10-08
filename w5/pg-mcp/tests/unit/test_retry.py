"""Unit tests for resilience.retry module.

Covers transient error detection, backoff computation, and the
``retry_with_backoff`` behaviour required by the implementation plan.
"""

from unittest.mock import AsyncMock

import pytest

from pg_mcp.config.settings import ResilienceConfig
from pg_mcp.resilience.retry import (
    compute_backoff_delay,
    is_transient_error,
    retry_with_backoff,
)


class TestIsTransientError:
    """Test transient error classification."""

    @pytest.mark.parametrize(
        "error_message",
        [
            "connection refused",
            "Query timed out",
            "request timed out after 30s",
            "service temporarily unavailable",
            "rate limit exceeded",
            "too many connections",
            "HTTP 429 Too Many Requests",
            "upstream returned 502",
            "503 Service Unavailable",
            "gateway timeout 504",
        ],
    )
    def test_transient_messages(self, error_message: str) -> None:
        assert is_transient_error(Exception(error_message)) is True

    @pytest.mark.parametrize(
        "error_message",
        [
            "syntax error at or near 'SELEC'",
            "relation 'users' does not exist",
            "permission denied for table users",
            "invalid API key",
        ],
    )
    def test_permanent_messages(self, error_message: str) -> None:
        assert is_transient_error(Exception(error_message)) is False

    def test_timeout_error_type(self) -> None:
        assert is_transient_error(TimeoutError()) is True

    def test_connection_error_type(self) -> None:
        assert is_transient_error(ConnectionError()) is True


class TestComputeBackoffDelay:
    """Test exponential backoff computation."""

    def test_first_attempt_uses_base_delay(self) -> None:
        assert compute_backoff_delay(0, 1.0, 2.0) == 1.0

    def test_exponential_growth(self) -> None:
        assert compute_backoff_delay(1, 1.0, 2.0) == 2.0
        assert compute_backoff_delay(2, 1.0, 2.0) == 4.0
        assert compute_backoff_delay(3, 1.0, 2.0) == 8.0

    def test_custom_base_and_factor(self) -> None:
        assert compute_backoff_delay(2, 0.5, 3.0) == pytest.approx(4.5)

    def test_factor_of_one_is_constant(self) -> None:
        assert compute_backoff_delay(5, 2.0, 1.0) == 2.0


class TestRetryWithBackoff:
    """Test retry_with_backoff behaviour."""

    async def test_success_first_attempt_no_retries(self) -> None:
        operation = AsyncMock(return_value="ok")
        sleep = AsyncMock()

        result = await retry_with_backoff(
            operation,
            config=ResilienceConfig(max_retries=3),
            operation_name="test_op",
            sleep=sleep,
        )

        assert result == "ok"
        assert operation.await_count == 1
        sleep.assert_not_awaited()

    async def test_retries_transient_failure_then_succeeds(self) -> None:
        operation = AsyncMock(side_effect=[ConnectionError("connection refused"), "recovered"])
        sleep = AsyncMock()

        result = await retry_with_backoff(
            operation,
            max_retries=3,
            base_delay=1.0,
            backoff_factor=2.0,
            operation_name="test_op",
            sleep=sleep,
        )

        assert result == "recovered"
        assert operation.await_count == 2
        sleep.assert_awaited_once_with(1.0)

    async def test_exponential_backoff_delays(self) -> None:
        operation = AsyncMock(
            side_effect=[
                TimeoutError(),
                TimeoutError(),
                TimeoutError(),
                "done",
            ]
        )
        sleep = AsyncMock()

        result = await retry_with_backoff(
            operation,
            max_retries=5,
            base_delay=0.5,
            backoff_factor=2.0,
            operation_name="test_op",
            sleep=sleep,
        )

        assert result == "done"
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0, 2.0]

    async def test_does_not_retry_permanent_failure(self) -> None:
        operation = AsyncMock(side_effect=ValueError("invalid input"))
        sleep = AsyncMock()

        with pytest.raises(ValueError, match="invalid input"):
            await retry_with_backoff(
                operation,
                max_retries=3,
                operation_name="test_op",
                sleep=sleep,
            )

        assert operation.await_count == 1
        sleep.assert_not_awaited()

    async def test_raises_after_exhausting_retries(self) -> None:
        operation = AsyncMock(side_effect=TimeoutError("still timing out"))
        sleep = AsyncMock()

        with pytest.raises(TimeoutError):
            await retry_with_backoff(
                operation,
                max_retries=2,
                base_delay=1.0,
                backoff_factor=2.0,
                operation_name="test_op",
                sleep=sleep,
            )

        # Initial attempt + 2 retries
        assert operation.await_count == 3
        assert sleep.await_count == 2

    async def test_zero_retries_fails_immediately(self) -> None:
        operation = AsyncMock(side_effect=ConnectionError("down"))
        sleep = AsyncMock()

        with pytest.raises(ConnectionError):
            await retry_with_backoff(
                operation,
                max_retries=0,
                operation_name="test_op",
                sleep=sleep,
            )

        assert operation.await_count == 1
        sleep.assert_not_awaited()

    async def test_config_provides_defaults(self) -> None:
        """ResilienceConfig fields drive retries, delays and backoff."""
        operation = AsyncMock(
            side_effect=[ConnectionError("connection lost"), ConnectionError("again"), "ok"]
        )
        sleep = AsyncMock()
        config = ResilienceConfig(max_retries=2, retry_delay=0.25, backoff_factor=4.0)

        result = await retry_with_backoff(
            operation,
            config=config,
            operation_name="test_op",
            sleep=sleep,
        )

        assert result == "ok"
        assert [call.args[0] for call in sleep.await_args_list] == [0.25, 1.0]

    async def test_explicit_params_override_config(self) -> None:
        operation = AsyncMock(side_effect=[TimeoutError(), "ok"])
        sleep = AsyncMock()
        config = ResilienceConfig(max_retries=0, retry_delay=5.0, backoff_factor=8.0)

        result = await retry_with_backoff(
            operation,
            config=config,
            max_retries=1,
            base_delay=0.1,
            backoff_factor=1.0,
            operation_name="test_op",
            sleep=sleep,
        )

        assert result == "ok"
        sleep.assert_awaited_once_with(0.1)

    async def test_no_config_defaults_to_no_retry(self) -> None:
        operation = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(TimeoutError):
            await retry_with_backoff(operation, operation_name="test_op")

        assert operation.await_count == 1
