"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_list_field(value: str | list[str]) -> list[str]:
    """Parse a comma-separated string or list into a list of strings.

    Args:
        value: Raw configuration value (comma-separated string or list).

    Returns:
        list[str]: Parsed list of non-empty, stripped entries.
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class DatabaseType(StrEnum):
    """Supported database engine types."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


class DatabaseConfig(BaseSettings):
    """Database connection configuration (PostgreSQL or MySQL)."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")
    db_type: DatabaseType = Field(
        default=DatabaseType.POSTGRESQL, description="Database engine type"
    )

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def is_mysql(self) -> bool:
        """Whether this config targets a MySQL database."""
        return self.db_type is DatabaseType.MYSQL

    @property
    def dsn(self) -> str:
        """Build DSN connection string for the configured engine."""
        scheme = "mysql" if self.is_mysql else "postgresql"
        return f"{scheme}://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        scheme = "mysql" if self.is_mysql else "postgresql"
        return f"{scheme}://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    base_url: str | None = Field(
        default=None, description="OpenAI-compatible API base URL (e.g. a proxy)"
    )
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(default=2000, ge=100, le=4096, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty and has correct format."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        if not api_key_str.startswith("sk-"):
            raise ValueError("OpenAI API key must start with 'sk-'")
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    allow_write_operations: bool = Field(
        default=False, description="Allow write operations (INSERT, UPDATE, DELETE)"
    )
    blocked_functions: list[str] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: list[str] = Field(
        default_factory=list,
        description="List of tables that must never be queried",
    )
    blocked_columns: list[str] = Field(
        default_factory=list,
        description="List of columns that must never be selected",
    )
    allow_explain: bool = Field(
        default=False, description="Allow EXPLAIN statements (read-only query plans)"
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("blocked_functions", mode="before")
    @classmethod
    def parse_blocked_functions(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        return parse_list_field(v)

    @field_validator("blocked_tables", mode="before")
    @classmethod
    def parse_blocked_tables(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        return parse_list_field(v)

    @field_validator("blocked_columns", mode="before")
    @classmethod
    def parse_blocked_columns(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        return parse_list_field(v)


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )
    min_confidence_score: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence score (0-100)"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )
    query_rate_limit: int = Field(
        default=10, ge=1, le=1000, description="Maximum concurrent database queries"
    )
    llm_rate_limit: int = Field(
        default=5, ge=1, le=1000, description="Maximum concurrent LLM calls"
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="text", description="Log format")


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    databases: list[DatabaseConfig] = Field(
        default_factory=list,
        description="All configured databases (first entry is the primary/default one)",
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def sync_databases(self) -> "Settings":
        """Keep ``databases`` and ``database`` consistent.

        ``database`` remains the primary/legacy single-database config (populated
        from ``DATABASE_*`` env vars). When ``databases`` is empty it is derived
        from ``database``; otherwise the first entry becomes the primary database.
        """
        if not self.databases:
            self.databases = [self.database]

        names = [db.name for db in self.databases]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate database names configured: {duplicates}")

        if self.database.name != self.databases[0].name:
            self.database = self.databases[0]

        return self

    def get_database(self, name: str) -> DatabaseConfig | None:
        """Look up a database configuration by name.

        Args:
            name: Configured database name.

        Returns:
            DatabaseConfig | None: Matching configuration or None if not found.
        """
        for db_config in self.databases:
            if db_config.name == name:
                return db_config
        return None

    @property
    def database_names(self) -> list[str]:
        """Names of all configured databases."""
        return [db.name for db in self.databases]

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None
