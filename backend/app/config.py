"""Runtime configuration, loaded from environment variables / backend/.env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"

    # Observability (LangSmith). Traces are sent only when both are set.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "finance-advisor-agent"
    langsmith_endpoint: str = ""  # e.g. https://eu.api.smith.langchain.com for EU accounts

    # Short-term memory (Redis). If Redis is unreachable we fall back to an
    # in-process store so local development still works.
    redis_url: str = "redis://localhost:6379/0"
    conversation_ttl_seconds: int = 60 * 60 * 24  # 24h
    max_history_messages: int = 20

    # SEC EDGAR requires a descriptive User-Agent with contact info.
    sec_user_agent: str = "FinanceAgent admin@example.com"
    sec_max_rps: float = 8.0  # EDGAR hard limit is 10 req/s

    # Quote provider: "yahoo" (default free source) or "mock". Plug your own
    # provider in app/services/quote.py and select it here.
    quote_provider: str = "yahoo"

    # How many filings of each form the filings workflow looks at.
    form4_lookback: int = 10
    eightk_lookback: int = 3

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
