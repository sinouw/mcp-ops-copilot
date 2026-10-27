"""Runtime configuration, loaded from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["scripted", "scripted-hijacked", "openai", "gemini", "groq", "openai-compatible"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://ops:ops@localhost:5432/ops"

    # LLM provider. "scripted" is a deterministic offline policy used by tests and CI evals;
    # "scripted-hijacked" simulates a fully prompt-injected model to stress the guardrails.
    llm_provider: Provider = "scripted"
    llm_model: str = "scripted-v1"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.0
    llm_timeout_s: float = 60.0
    # USD per million tokens; leave at 0 unless you know your provider's current price.
    llm_input_cost_per_mtok: float = 0.0
    llm_output_cost_per_mtok: float = 0.0

    # MCP servers. Empty URL = spawn the server as a stdio subprocess.
    mcp_crm_url: str = ""
    mcp_ticketing_url: str = ""

    # Policy
    refund_approval_threshold_cents: int = 10_000
    refund_hard_cap_cents: int = 100_000
    support_email: str = "support@acme-shop.example"

    # Budgets per run
    max_llm_calls: int = 30
    max_tool_calls: int = 20
    max_total_tokens: int = 120_000
    max_cost_usd: float = 0.25
    max_ticket_chars: int = 4_000

    # API
    reviewer_token: str = Field(default="", description="If set, approval decisions need it.")
    cors_origins: str = "http://localhost:5173"

    otel_service_name: str = "mcp-ops-copilot"
    otel_exporter_otlp_endpoint: str = ""
    otel_console: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
