"""
config/settings.py
------------------
Pydantic BaseSettings for oracle-gl-reconciliation-agent.

All settings are loaded from environment variables (or a .env file at
project root).  The nested model pattern keeps Oracle, Agent, and
Notification concerns cleanly separated.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Oracle Fusion Cloud connection settings
# ---------------------------------------------------------------------------

class OracleSettings(BaseSettings):
    """Oracle ERP Cloud REST API connection parameters."""

    model_config = SettingsConfigDict(
        env_prefix="ORACLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base URL — no trailing slash
    host: str = Field(
        ...,
        description="Oracle Fusion Cloud base URL, e.g. https://your-instance.fa.us2.oraclecloud.com",
    )

    # Basic auth (fallback when OAuth is unavailable / not configured)
    username: Optional[str] = Field(
        default=None,
        description="Oracle Fusion service account username",
    )
    password: Optional[str] = Field(
        default=None,
        description="Oracle Fusion service account password",
    )

    # OAuth 2.0 Client Credentials
    client_id: Optional[str] = Field(
        default=None,
        description="IDCS / OCI IAM OAuth 2.0 client_id",
    )
    client_secret: Optional[str] = Field(
        default=None,
        description="IDCS / OCI IAM OAuth 2.0 client_secret",
    )
    token_url: Optional[str] = Field(
        default=None,
        description="OAuth2 token endpoint URL",
    )

    # GL context defaults
    ledger_id: int = Field(
        ...,
        description="GL_LEDGERS.LEDGER_ID for the primary ledger to reconcile",
    )
    period_name: str = Field(
        ...,
        description="GL_PERIODS.PERIOD_NAME, e.g. Jan-25",
    )

    # REST API version segment (update if Oracle ships newer)
    api_version: str = Field(
        default="11.13.18.05",
        description="fscmRestApi version path segment",
    )

    # Connection timeouts (seconds)
    connect_timeout: int = Field(default=10)
    read_timeout: int = Field(default=60)

    @field_validator("host")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


# ---------------------------------------------------------------------------
# Agent / LLM settings
# ---------------------------------------------------------------------------

class AgentSettings(BaseSettings):
    """LLM provider and agent loop configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider: Literal["claude", "openai"] = Field(
        default="claude",
        alias="AGENT_PROVIDER",
        description="LLM provider to use: claude | openai",
    )

    # Anthropic
    anthropic_api_key: Optional[str] = Field(
        default=None,
        alias="ANTHROPIC_API_KEY",
    )
    claude_model: str = Field(
        default="claude-opus-4-5",
        alias="CLAUDE_MODEL",
        description="Anthropic model ID",
    )

    # OpenAI
    openai_api_key: Optional[str] = Field(
        default=None,
        alias="OPENAI_API_KEY",
    )
    openai_model: str = Field(
        default="gpt-4o",
        alias="OPENAI_MODEL",
        description="OpenAI model ID",
    )

    # Agentic loop controls
    max_tool_rounds: int = Field(
        default=20,
        alias="MAX_TOOL_ROUNDS",
        description="Maximum tool-calling rounds before the agent stops",
    )
    temperature: float = Field(
        default=0.0,
        alias="AGENT_TEMPERATURE",
        description="LLM sampling temperature; 0.0 = deterministic",
    )

    output_dir: str = Field(
        default="output",
        alias="OUTPUT_DIR",
        description="Directory for FBDI files and HTML reports",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------

class NotificationSettings(BaseSettings):
    """SMTP / email settings for approval routing."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    approver_email: str = Field(
        default="gl-controller@yourcompany.com",
        alias="APPROVER_EMAIL",
    )
    smtp_host: str = Field(
        default="smtp.yourcompany.com",
        alias="SMTP_HOST",
    )
    smtp_port: int = Field(
        default=587,
        alias="SMTP_PORT",
    )
    smtp_username: Optional[str] = Field(
        default=None,
        alias="SMTP_USERNAME",
    )
    smtp_password: Optional[str] = Field(
        default=None,
        alias="SMTP_PASSWORD",
    )
    smtp_use_tls: bool = Field(
        default=True,
        alias="SMTP_USE_TLS",
    )
    from_address: str = Field(
        default="gl-agent@yourcompany.com",
        alias="SMTP_USERNAME",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Convenience: load all settings at once
# ---------------------------------------------------------------------------

def load_settings() -> tuple[OracleSettings, AgentSettings, NotificationSettings]:
    """Load and return all three settings objects from environment / .env."""
    oracle = OracleSettings()
    agent = AgentSettings()
    notification = NotificationSettings()
    return oracle, agent, notification
