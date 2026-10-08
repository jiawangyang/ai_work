"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def isolate_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow the developer ``.env`` file so tests are hermetic.

    Environment variables take priority over dotenv values in
    pydantic-settings, so setting ``DATABASES`` to an empty JSON array
    prevents a developer's real ``.env`` from leaking into tests.
    """
    monkeypatch.setenv("DATABASES", "[]")


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics for tests to avoid port conflicts."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]
