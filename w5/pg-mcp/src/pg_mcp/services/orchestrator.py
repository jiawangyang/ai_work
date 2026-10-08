"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements retry logic, error handling, and request tracking.
"""

import time
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import DatabaseConfig, ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    PgMcpError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import (
    clear_request_id,
    generate_request_id,
    get_tracing_logger,
    set_request_id,
)
from pg_mcp.resilience.circuit_breaker import CircuitBreaker, CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.resilience.retry import retry_with_backoff
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_tracing_logger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. It implements retry logic with error feedback, circuit breaker
    pattern for fault tolerance, and comprehensive error handling.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     sql_executor=executor,
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor | None,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        *,
        sql_executors: dict[str, SQLExecutor] | None = None,
        sql_generators: dict[str, SQLGenerator] | None = None,
        sql_validators: dict[str, SQLValidator] | None = None,
        db_configs: dict[str, DatabaseConfig] | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executor: Fallback SQL execution service (single-database setups).
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries and circuit breaker.
            validation_config: Validation configuration including thresholds.
            sql_executors: Per-database executors; the executor matching the
                resolved database is always preferred over ``sql_executor``.
            sql_generators: Per-database generators (e.g. MySQL dialect prompts),
                preferred over ``sql_generator`` when present.
            sql_validators: Per-database validators (e.g. MySQL dialect),
                preferred over ``sql_validator`` when present.
            db_configs: Per-database configurations used to introspect with
                the correct engine type when loading schemas.
            rate_limiter: Optional rate limiter applied to LLM and DB operations.
            metrics: Optional metrics collector (defaults to the global one).
            circuit_breaker: Optional shared circuit breaker. When omitted, a
                dedicated one is created from ``resilience_config``.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executor = sql_executor
        self.sql_executors = sql_executors or {}
        self.sql_generators = sql_generators or {}
        self.sql_validators = sql_validators or {}
        self.db_configs = db_configs or {}
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter
        self.metrics = metrics if metrics is not None else MetricsCollector()

        # Share a single circuit breaker when one is provided by the caller
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Generate request_id for tracking
        2. Resolve and validate database name
        3. Load schema from cache
        4. Generate and validate SQL with retry logic
        5. Execute SQL (if return_type == RESULT)
        6. Validate results (optional)
        7. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        # Generate request_id for full-chain tracing and bind it to the context
        request_id = generate_request_id()
        set_request_id(request_id)
        started_at = time.time()
        database_name = request.database or "unknown"

        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        try:
            # Step 0: Reject oversized questions before spending LLM tokens
            self._validate_question_length(request.question, request_id)

            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    db_type = self.db_configs.get(database_name)
                    schema = await self.schema_cache.load(
                        database_name, pool, db_type.db_type if db_type else "postgresql"
                    )
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
                database_name=database_name,
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                self._record_request_metrics("success", database_name, started_at)
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                )

            # Step 5: Execute SQL using the executor bound to the resolved database
            logger.debug("Executing SQL", extra={"request_id": request_id})
            executor = self._get_executor(database_name)

            results, total_count, execution_time_ms = await self._execute_with_resilience(
                executor=executor,
                sql=generated_sql,
                request_id=request_id,
            )
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )
            self._record_request_metrics("success", database_name, started_at)

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 6b: Surface low-confidence results instead of silently trusting them
            if result_confidence < self.validation_config.min_confidence_score:
                logger.warning(
                    "Result confidence below configured threshold",
                    extra={
                        "request_id": request_id,
                        "confidence": result_confidence,
                        "min_confidence_score": self.validation_config.min_confidence_score,
                    },
                )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
            )

        except PgMcpError as e:
            # Handle known application errors
            self._record_request_metrics("error", database_name, started_at)
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code.value,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            # Handle unexpected errors
            self._record_request_metrics("error", database_name, started_at)
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )

        finally:
            clear_request_id()

    def _validate_question_length(self, question: str, request_id: str) -> None:
        """Reject questions longer than the configured maximum.

        Args:
            question: Raw user question.
            request_id: Request ID for logging context.

        Raises:
            PgMcpError: With ``ErrorCode.QUESTION_TOO_LONG`` when too long.
        """
        max_length = self.validation_config.max_question_length
        if len(question) > max_length:
            raise PgMcpError(
                message=f"Question exceeds maximum length of {max_length} characters",
                code=ErrorCode.QUESTION_TOO_LONG,
                details={
                    "request_id": request_id,
                    "question_length": len(question),
                    "max_question_length": max_length,
                },
            )

    def _get_executor(self, database_name: str) -> SQLExecutor:
        """Return the SQL executor bound to the given database.

        Per-database executors take precedence; the legacy single executor is
        used as a fallback so single-database setups keep working.

        Args:
            database_name: Resolved target database name.

        Returns:
            SQLExecutor: Executor bound to that database.

        Raises:
            DatabaseError: If no executor is configured for the database.
        """
        executor = self.sql_executors.get(database_name)
        if executor is None:
            executor = self.sql_executor
        if executor is None:
            raise DatabaseError(
                message=f"No SQL executor available for database '{database_name}'",
                details={
                    "database": database_name,
                    "configured_executors": sorted(self.sql_executors),
                },
            )
        return executor

    def _get_generator(self, database_name: str) -> SQLGenerator:
        """Return the SQL generator bound to the given database.

        Args:
            database_name: Resolved target database name.

        Returns:
            SQLGenerator: Per-database generator or the shared fallback.
        """
        return self.sql_generators.get(database_name) or self.sql_generator

    def _get_validator(self, database_name: str) -> SQLValidator:
        """Return the SQL validator bound to the given database.

        Args:
            database_name: Resolved target database name.

        Returns:
            SQLValidator: Per-database validator or the shared fallback.
        """
        return self.sql_validators.get(database_name) or self.sql_validator

    async def _execute_with_resilience(
        self,
        executor: SQLExecutor,
        sql: str,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], int, float]:
        """Execute SQL with rate limiting, retry/backoff and metrics.

        Args:
            executor: Executor bound to the target database.
            sql: Validated SQL statement.
            request_id: Request ID for logging context.

        Returns:
            tuple: (rows, total_count, execution_time_ms)
        """

        async def run() -> tuple[list[dict[str, Any]], int]:
            if self.rate_limiter is None:
                return await executor.execute(sql)
            async with self.rate_limiter.for_queries():
                return await executor.execute(sql)

        start_time = self._get_current_time_ms()
        results, total_count = await retry_with_backoff(
            run,
            config=self.resilience_config,
            operation_name="execute_sql",
        )
        execution_time_ms = self._get_current_time_ms() - start_time

        if self.metrics is not None:
            self.metrics.observe_db_query_duration(execution_time_ms / 1000)

        logger.info(
            "SQL executed successfully",
            extra={
                "request_id": request_id,
                "row_count": total_count,
                "execution_time_ms": execution_time_ms,
            },
        )

        return results, total_count, execution_time_ms

    def _record_request_metrics(self, status: str, database: str, started_at: float) -> None:
        """Record request count and duration metrics.

        Args:
            status: Outcome label (success, error, ...).
            database: Target database name.
            started_at: Monotonic start timestamp of the request.
        """
        if self.metrics is None:
            return
        self.metrics.increment_query_request(status=status, database=database)
        self.metrics.query_duration.observe(time.time() - started_at)

    def _build_validation_result(
        self, sql: str, validator: SQLValidator | None = None
    ) -> ValidationResult:
        """Build a validation result from the validator's actual outcome.

        Falls back to a passing result when the validator does not expose the
        detailed API (e.g. lightweight test doubles).

        Args:
            sql: Generated SQL statement.
            validator: Validator to inspect (defaults to the shared one).

        Returns:
            ValidationResult: Structured validation outcome.
        """
        validator = validator or self.sql_validator
        detailed = getattr(validator, "validate_detailed", None)
        if callable(detailed):
            result = detailed(sql)
            if isinstance(result, ValidationResult):
                return result

        return ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )

    async def health(self) -> dict[str, Any]:
        """Return a health snapshot of the orchestrator and its dependencies.

        Returns:
            dict: Status plus database/executor inventory, circuit breaker and
                rate limiter statistics.
        """
        circuit_state = self.circuit_breaker.state
        status = "degraded" if (not self.pools or circuit_state == CircuitState.OPEN) else "healthy"

        return {
            "status": status,
            "databases": sorted(self.pools),
            "executors": sorted(self.sql_executors),
            "circuit_breaker": self.circuit_breaker.get_stats(),
            "rate_limiter": self.rate_limiter.get_all_stats() if self.rate_limiter else None,
            "validation_enabled": self.validation_config.enabled,
        }

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _generate_with_resilience(
        self,
        generator: SQLGenerator,
        question: str,
        schema: Any,
        previous_attempt: str | None,
        error_feedback: str | None,
    ) -> str:
        """Call the SQL generator with rate limiting, retry/backoff and metrics.

        Args:
            generator: Generator bound to the target database.
            question: User's natural language question.
            schema: Database schema for context.
            previous_attempt: Previously generated SQL (if retrying).
            error_feedback: Validation error feedback (if retrying).

        Returns:
            str: Generated SQL statement.
        """

        async def call() -> str:
            if self.rate_limiter is None:
                return await generator.generate(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_attempt,
                    error_feedback=error_feedback,
                )
            async with self.rate_limiter.for_llm():
                return await generator.generate(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_attempt,
                    error_feedback=error_feedback,
                )

        started_at = time.time()
        try:
            return await retry_with_backoff(
                call,
                config=self.resilience_config,
                operation_name="generate_sql",
            )
        finally:
            if self.metrics is not None:
                self.metrics.increment_llm_call(operation="generate_sql")
                self.metrics.observe_llm_latency("generate_sql", time.time() - started_at)

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
        database_name: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry logic on validation failures.

        This method implements a retry loop that:
        1. Checks circuit breaker state
        2. Generates SQL using LLM
        3. Validates the generated SQL
        4. On validation failure, retries with error feedback
        5. Records success/failure to circuit breaker

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.
            database_name: Resolved target database name.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     request_id="123",
            ...     database_name="mydb",
            ... )
        """
        generator = self._get_generator(database_name)
        validator = self._get_validator(database_name)

        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL (rate limited, retried on transient LLM failures)
                generated_sql = await self._generate_with_resilience(
                    generator=generator,
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=error_feedback,
                )

                # Note: tokens_used would come from OpenAI response metadata if available
                # For now, we don't extract it, but it can be added later

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL
                try:
                    validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    if attempt < max_retries:
                        # Record as failure and retry with feedback
                        if self.metrics is not None:
                            self.metrics.increment_sql_rejected(reason="validation_failed")
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        continue
                    else:
                        # Out of retries, record failure and raise
                        self.circuit_breaker.record_failure()
                        logger.error(
                            "SQL validation failed after all retries",
                            extra={
                                "request_id": request_id,
                                "attempts": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                # Build validation result from the validator's actual outcome
                validation_result = self._build_validation_result(generated_sql, validator)

                return generated_sql, validation_result, tokens_used

            except (LLMError, SecurityViolationError, SQLParseError):
                # Re-raise known errors
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with configured minimum confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return self.validation_config.min_confidence_score

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        import time

        return time.time() * 1000
