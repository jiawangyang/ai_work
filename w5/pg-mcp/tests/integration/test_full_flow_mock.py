"""Mock-based integration test for the full query pipeline.

Unlike ``test_full_flow.py`` (which requires a live PostgreSQL + OpenAI
endpoint), this module wires the orchestrator with fully mocked components so
the complete request path — question length guard, database resolution, schema
lookup, SQL generation/validation retries, executor routing, rate limiting,
metrics, and result validation — is exercised deterministically in CI.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import ErrorCode
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator


@pytest.fixture
def mock_schema() -> DatabaseSchema:
    """Schema for two databases, each with a users table."""
    users_table = TableInfo(
        schema_name="public",
        table_name="users",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True),
            ColumnInfo(name="email", data_type="varchar(255)", is_nullable=False),
        ],
    )
    return DatabaseSchema(
        database_name="shop_db",
        tables=[users_table],
        version="15.0",
    )


def build_orchestrator(
    mock_schema: DatabaseSchema,
    *,
    generated_sql: str = "SELECT COUNT(*) AS total FROM users",
    rows: list[dict] | None = None,
    result_confidence: int = 95,
) -> tuple[QueryOrchestrator, dict[str, AsyncMock]]:
    """Wire an orchestrator with fully mocked collaborators."""
    rows = rows if rows is not None else [{"total": 42}]

    generator = MagicMock()
    generator.generate = AsyncMock(return_value=generated_sql)

    validator = MagicMock()
    validator.validate_or_raise = MagicMock(return_value=None)
    validator.validate_detailed = MagicMock(
        return_value=ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )
    )

    executor = MagicMock()
    executor.execute = AsyncMock(return_value=(rows, len(rows)))

    result_validator = MagicMock()
    result_validator.validate = AsyncMock(
        return_value=ResultValidationResult(
            confidence=result_confidence,
            explanation="Results match the question",
            suggestion=None,
            is_acceptable=result_confidence >= 70,
        )
    )

    cache = MagicMock()
    cache.get = MagicMock(return_value=mock_schema)

    rate_limiter = MultiRateLimiter(query_limit=2, llm_limit=2)

    orchestrator = QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        sql_executor=None,  # No fallback: routing must go through sql_executors
        result_validator=result_validator,
        schema_cache=cache,
        pools={"shop_db": MagicMock()},
        resilience_config=ResilienceConfig(max_retries=2, retry_delay=0.1),
        validation_config=ValidationConfig(enabled=True, min_confidence_score=70),
        sql_executors={"shop_db": executor},
        rate_limiter=rate_limiter,
    )

    mocks = {
        "generator": generator,
        "validator": validator,
        "executor": executor,
        "result_validator": result_validator,
        "cache": cache,
    }
    return orchestrator, mocks


class TestFullPipeline:
    """End-to-end pipeline tests with mocked collaborators."""

    @pytest.mark.asyncio
    async def test_happy_path_result_flow(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, mocks = build_orchestrator(mock_schema)

        response = await orchestrator.execute_query(
            QueryRequest(question="How many users are there?", database="shop_db")
        )

        assert response.success is True
        assert response.generated_sql == "SELECT COUNT(*) AS total FROM users"
        assert response.data is not None
        assert response.data.rows == [{"total": 42}]
        assert response.data.row_count == 1
        assert response.confidence == 95
        assert response.error is None
        assert response.validation is not None
        assert response.validation.is_valid is True

        # All pipeline stages ran
        mocks["cache"].get.assert_called_once_with("shop_db")
        mocks["generator"].generate.assert_awaited_once()
        mocks["executor"].execute.assert_awaited_once()
        mocks["result_validator"].validate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sql_only_flow_skips_execution(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, mocks = build_orchestrator(mock_schema)

        response = await orchestrator.execute_query(
            QueryRequest(question="Count users", database="shop_db", return_type=ReturnType.SQL)
        )

        assert response.success is True
        assert response.data is None
        assert response.confidence == 100
        mocks["executor"].execute.assert_not_awaited()
        mocks["result_validator"].validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_database_fails_gracefully(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, _ = build_orchestrator(mock_schema)

        response = await orchestrator.execute_query(
            QueryRequest(question="Count users", database="nonexistent_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == ErrorCode.DATABASE_ERROR.value

    @pytest.mark.asyncio
    async def test_invalid_sql_retries_then_succeeds(self, mock_schema: DatabaseSchema) -> None:
        """First generated SQL violates security, retry with feedback succeeds."""
        orchestrator, mocks = build_orchestrator(mock_schema)

        bad_attempt = "DELETE FROM users"
        good_attempt = "SELECT COUNT(*) AS total FROM users"

        mocks["generator"].generate = AsyncMock(side_effect=[bad_attempt, good_attempt])
        mocks["validator"].validate_or_raise = MagicMock(
            side_effect=[
                _make_security_violation("DELETE not allowed"),
                None,
            ]
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="Remove all users", database="shop_db")
        )

        assert response.success is True
        assert response.generated_sql == good_attempt
        assert mocks["generator"].generate.await_count == 2
        # The retry included the previous attempt and error feedback
        retry_kwargs = mocks["generator"].generate.await_args_list[1].kwargs
        assert retry_kwargs["previous_attempt"] == bad_attempt
        assert "DELETE" in retry_kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_security_violation_after_retries_fails_request(
        self, mock_schema: DatabaseSchema
    ) -> None:
        orchestrator, mocks = build_orchestrator(mock_schema)

        mocks["validator"].validate_or_raise = MagicMock(
            side_effect=_make_security_violation("blocked function pg_sleep")
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="Sleep", database="shop_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == ErrorCode.SECURITY_VIOLATION.value
        # Initial attempt + max_retries retries
        assert mocks["generator"].generate.await_count == 3

    @pytest.mark.asyncio
    async def test_oversized_question_rejected_before_llm(
        self, mock_schema: DatabaseSchema
    ) -> None:
        orchestrator, mocks = build_orchestrator(mock_schema)
        orchestrator.validation_config = ValidationConfig(max_question_length=5)

        response = await orchestrator.execute_query(
            QueryRequest(question="way too long question", database="shop_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == ErrorCode.QUESTION_TOO_LONG.value
        mocks["generator"].generate.assert_not_awaited()
        mocks["executor"].execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_confidence_result_surfaced(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, _ = build_orchestrator(mock_schema, result_confidence=40)

        response = await orchestrator.execute_query(
            QueryRequest(question="Count users", database="shop_db")
        )

        # The query still succeeds but the low confidence is surfaced
        assert response.success is True
        assert response.confidence == 40

    @pytest.mark.asyncio
    async def test_execution_failure_returns_error_response(
        self, mock_schema: DatabaseSchema
    ) -> None:
        from pg_mcp.models.errors import DatabaseError

        orchestrator, mocks = build_orchestrator(mock_schema)
        mocks["executor"].execute = AsyncMock(
            side_effect=DatabaseError("connection to server lost")
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="Count users", database="shop_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == ErrorCode.DATABASE_ERROR.value

    @pytest.mark.asyncio
    async def test_rate_limiter_released_after_request(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, _ = build_orchestrator(mock_schema)

        response = await orchestrator.execute_query(
            QueryRequest(question="Count users", database="shop_db")
        )

        assert response.success is True
        stats = orchestrator.rate_limiter.get_all_stats()  # type: ignore[union-attr]
        assert stats["queries"]["active_count"] == 0
        assert stats["llm"]["active_count"] == 0

    @pytest.mark.asyncio
    async def test_health_reflects_components(self, mock_schema: DatabaseSchema) -> None:
        orchestrator, _ = build_orchestrator(mock_schema)

        health = await orchestrator.health()

        assert health["status"] == "healthy"
        assert health["databases"] == ["shop_db"]
        assert health["executors"] == ["shop_db"]
        assert health["circuit_breaker"]["state"] == CircuitState.CLOSED.value
        assert health["validation_enabled"] is True


def _make_security_violation(message: str) -> Exception:
    from pg_mcp.models.errors import SecurityViolationError

    return SecurityViolationError(message)
