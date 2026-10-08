"""Unit tests for observability modules (metrics + tracing)."""

import logging

import pytest

from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import (
    clear_request_id,
    generate_request_id,
    get_request_id,
    get_tracing_logger,
    request_context,
    set_request_id,
    trace_async,
    trace_sync,
)


class TestMetricsCollector:
    """Test the Prometheus metrics collector."""

    def test_singleton_instance(self) -> None:
        assert MetricsCollector() is MetricsCollector()

    def test_increment_query_request(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_query_request(status="success", database="testdb")
        assert metrics.query_requests.labels(status="success", database="testdb")._value.get() == 1

    def test_increment_llm_call(self) -> None:
        # Unique label: prometheus metrics are process-global and other test
        # modules increment the same series names.
        metrics = MetricsCollector()
        metrics.increment_llm_call(operation="test_only_generate_sql")
        assert metrics.llm_calls.labels(operation="test_only_generate_sql")._value.get() == 1

    def test_observe_llm_latency(self) -> None:
        metrics = MetricsCollector()
        before = metrics.llm_latency.labels(operation="test_only_latency")._sum.get()
        metrics.observe_llm_latency("test_only_latency", 0.5)
        assert metrics.llm_latency.labels(operation="test_only_latency")._sum.get() == (
            pytest.approx(before + 0.5)
        )

    def test_increment_llm_tokens(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_llm_tokens(operation="generate_sql", tokens=150)
        assert metrics.llm_tokens_used.labels(operation="generate_sql")._value.get() == 150

    def test_increment_sql_rejected(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_sql_rejected(reason="test_only_validation_failed")
        assert metrics.sql_rejected.labels(reason="test_only_validation_failed")._value.get() == 1

    def test_set_db_connections_active(self) -> None:
        metrics = MetricsCollector()
        metrics.set_db_connections_active(database="testdb", count=7)
        assert metrics.db_connections_active.labels(database="testdb")._value.get() == 7

    def test_observe_db_query_duration(self) -> None:
        metrics = MetricsCollector()
        before = metrics.db_query_duration._sum.get()
        metrics.observe_db_query_duration(0.25)
        assert metrics.db_query_duration._sum.get() == pytest.approx(before + 0.25)

    def test_set_schema_cache_age(self) -> None:
        metrics = MetricsCollector()
        metrics.set_schema_cache_age(database="testdb", age_seconds=12.5)
        assert metrics.schema_cache_age.labels(database="testdb")._value.get() == 12.5


class TestRequestIdContext:
    """Test request ID generation and context propagation."""

    def test_generate_request_id_is_unique_uuid(self) -> None:
        id1 = generate_request_id()
        id2 = generate_request_id()
        assert id1 != id2
        assert len(id1) == 36

    def test_set_and_get_request_id(self) -> None:
        set_request_id("req-abc")
        assert get_request_id() == "req-abc"
        clear_request_id()
        assert get_request_id() is None

    async def test_request_context_sets_and_resets(self) -> None:
        clear_request_id()
        async with request_context("req-1") as request_id:
            assert request_id == "req-1"
            assert get_request_id() == "req-1"
        assert get_request_id() is None

    async def test_request_context_generates_id_when_missing(self) -> None:
        async with request_context() as request_id:
            assert request_id is not None
            assert get_request_id() == request_id

    async def test_request_context_resets_on_exception(self) -> None:
        clear_request_id()
        with pytest.raises(RuntimeError):
            async with request_context("req-err"):
                raise RuntimeError("boom")
        assert get_request_id() is None


class TestTracingLogger:
    """Test the request-context aware logger wrapper."""

    def test_injects_request_id_into_extra(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("test.tracing.logger")
        set_request_id("req-log-1")
        try:
            with caplog.at_level(logging.INFO, logger="test.tracing.logger"):
                logger.info("processing request")
        finally:
            clear_request_id()

        record = caplog.records[0]
        assert record.request_id == "req-log-1"

    def test_explicit_request_id_not_overwritten(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("test.tracing.logger")
        set_request_id("ctx-id")
        try:
            with caplog.at_level(logging.INFO, logger="test.tracing.logger"):
                logger.info("msg", extra={"request_id": "explicit-id"})
        finally:
            clear_request_id()

        assert caplog.records[0].request_id == "explicit-id"

    def test_no_context_no_injection(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("test.tracing.logger")
        clear_request_id()
        with caplog.at_level(logging.INFO, logger="test.tracing.logger"):
            logger.info("no context")

        assert not hasattr(caplog.records[0], "request_id")

    def test_level_methods(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("test.tracing.levels")
        with caplog.at_level(logging.DEBUG, logger="test.tracing.levels"):
            logger.debug("d")
            logger.warning("w")
            logger.error("e")
            logger.critical("c")

        messages = [r.message for r in caplog.records]
        assert messages == ["d", "w", "e", "c"]

    def test_exception_logs_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_tracing_logger("test.tracing.exc")
        with caplog.at_level(logging.ERROR, logger="test.tracing.exc"):
            try:
                raise ValueError("boom")
            except ValueError:
                logger.exception("caught error")

        record = caplog.records[0]
        assert record.exc_info is not None
        assert record.exc_info[0] is ValueError


class TestTraceDecorators:
    """Test the trace_async / trace_sync decorators."""

    async def test_trace_async_sets_record_attributes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.trace.async")

        @trace_async(operation="op_async")
        async def work() -> str:
            logger.info("inside work")
            return "ok"

        with caplog.at_level(logging.INFO, logger="test.trace.async"):
            async with request_context("req-decorator"):
                result = await work()

        assert result == "ok"
        record = caplog.records[0]
        assert record.request_id == "req-decorator"
        assert record.operation == "op_async"

    def test_trace_sync_sets_record_attributes(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("test.trace.sync")

        @trace_sync(operation="op_sync")
        def work() -> int:
            logger.info("inside sync work")
            return 42

        set_request_id("req-sync")
        try:
            with caplog.at_level(logging.INFO, logger="test.trace.sync"):
                result = work()
        finally:
            clear_request_id()

        assert result == 42
        record = caplog.records[0]
        assert record.request_id == "req-sync"
        assert record.operation == "op_sync"

    async def test_trace_async_without_context_still_works(self) -> None:
        clear_request_id()

        @trace_async()
        async def work() -> str:
            return "fine"

        assert await work() == "fine"

    def test_trace_sync_restores_log_record_factory(self) -> None:
        original_factory = logging.getLogRecordFactory()

        @trace_sync()
        def work() -> None:
            pass

        set_request_id("req-factory")
        try:
            work()
        finally:
            clear_request_id()

        assert logging.getLogRecordFactory() is original_factory
