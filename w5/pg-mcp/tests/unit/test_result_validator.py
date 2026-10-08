"""Unit tests for ResultValidator with a mocked OpenAI client.

These tests are fully deterministic: no real OpenAI API calls are made.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.services.result_validator import ResultValidator


def make_validator(
    validation_config: ValidationConfig | None = None,
) -> ResultValidator:
    """Create a ResultValidator with a mocked OpenAI client."""
    openai_config = OpenAIConfig(api_key="sk-test-key-123456")
    validator = ResultValidator(
        openai_config=openai_config,
        validation_config=validation_config or ValidationConfig(),
    )
    validator.client = AsyncMock()
    return validator


def make_completion(content: str | None, choices: int = 1) -> SimpleNamespace:
    """Build a fake ChatCompletion-like object."""

    class FakeResponse(SimpleNamespace):
        def model_dump(self) -> dict:
            return {"choices": len(self.choices)}

    choice = SimpleNamespace(message=SimpleNamespace(content=content))
    return FakeResponse(choices=[choice] * choices)


class TestValidationDisabled:
    """Tests for the disabled-validation shortcut."""

    async def test_disabled_returns_high_confidence(self) -> None:
        validator = make_validator(ValidationConfig(enabled=False))

        result = await validator.validate(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 1}],
            row_count=1,
        )

        assert result.confidence == 100
        assert result.is_acceptable is True
        validator.client.chat.completions.create.assert_not_called()


class TestSuccessfulValidation:
    """Tests for successful LLM validation."""

    async def test_parses_llm_json_response(self) -> None:
        validator = make_validator()
        payload = {
            "confidence": 92,
            "explanation": "Results answer the question",
            "suggestion": None,
        }
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps(payload))
        )

        result = await validator.validate(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
        )

        assert result.confidence == 92
        assert result.explanation == "Results answer the question"
        assert result.is_acceptable is True

    async def test_below_threshold_not_acceptable(self) -> None:
        validator = make_validator(ValidationConfig(confidence_threshold=80))
        payload = {"confidence": 55, "explanation": "Uncertain"}
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps(payload))
        )

        result = await validator.validate(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
        )

        assert result.confidence == 55
        assert result.is_acceptable is False

    async def test_out_of_range_confidence_is_clamped(self) -> None:
        validator = make_validator()
        payload = {"confidence": 250, "explanation": "Over"}
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps(payload))
        )

        result = await validator.validate(
            question="q",
            sql="SELECT 1",
            results=[],
            row_count=0,
        )

        assert result.confidence == 100

    async def test_non_numeric_confidence_defaults_to_50(self) -> None:
        validator = make_validator()
        payload = {"confidence": "high", "explanation": "Bad type"}
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps(payload))
        )

        result = await validator.validate(
            question="q",
            sql="SELECT 1",
            results=[],
            row_count=0,
        )

        assert result.confidence == 50

    async def test_missing_fields_use_defaults(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps({}))
        )

        result = await validator.validate(
            question="q",
            sql="SELECT 1",
            results=[],
            row_count=0,
        )

        assert result.confidence == 50
        assert result.explanation == "No explanation provided"

    async def test_sample_rows_limits_payload(self) -> None:
        validation_config = ValidationConfig(sample_rows=2)
        validator = make_validator(validation_config)
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion(json.dumps({"confidence": 90}))
        )

        rows = [{"id": i} for i in range(10)]
        await validator.validate(
            question="q",
            sql="SELECT * FROM t",
            results=rows,
            row_count=10,
        )

        # The user prompt must only contain the sampled rows
        call_kwargs = validator.client.chat.completions.create.await_args.kwargs
        user_message = call_kwargs["messages"][1]["content"]
        assert '"id": 0' in user_message
        assert '"id": 1' in user_message
        assert '"id": 2' not in user_message


class TestMalformedResponses:
    """Tests for malformed LLM responses."""

    async def test_invalid_json_returns_moderate_confidence(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            return_value=make_completion("not json at all")
        )

        result = await validator.validate(
            question="q",
            sql="SELECT 1",
            results=[],
            row_count=0,
        )

        assert result.confidence == 60
        assert result.is_acceptable is False
        assert "parsing failed" in result.explanation.lower()

    async def test_empty_choices_raises_llm_error(self) -> None:
        validator = make_validator()
        empty_completion = make_completion("ignored")
        empty_completion.choices = []
        validator.client.chat.completions.create = AsyncMock(return_value=empty_completion)

        with pytest.raises(LLMError, match="empty response"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )

    async def test_empty_content_raises_llm_error(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(return_value=make_completion(None))

        with pytest.raises(LLMError, match="empty message content"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )


class TestErrorMapping:
    """Tests for exception mapping to LLM error types."""

    async def test_timeout_raises_llm_timeout_error(self) -> None:
        validator = make_validator(ValidationConfig(timeout_seconds=5.0))
        validator.client.chat.completions.create = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(LLMTimeoutError, match=r"timed out after 5\.0s"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )

    async def test_authentication_error_raises_unavailable(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=Exception("Error: incorrect api_key provided")
        )

        with pytest.raises(LLMUnavailableError, match="authentication failed"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )

    async def test_rate_limit_error_raises_unavailable(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=Exception("rate_limit exceeded for requests")
        )

        with pytest.raises(LLMUnavailableError, match="rate limit"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )

    async def test_generic_error_raises_llm_error(self) -> None:
        validator = make_validator()
        validator.client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("something exploded")
        )

        with pytest.raises(LLMError, match="something exploded"):
            await validator.validate(
                question="q",
                sql="SELECT 1",
                results=[],
                row_count=0,
            )
