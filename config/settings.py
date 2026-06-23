from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ───────────────────────────────────────────────────────────────────
    gemini_api_key: str = Field(default="", description="Google Gemini API key from AI Studio")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", description="Gemini model identifier")

    # ── RabbitMQ ──────────────────────────────────────────────────────────────
    rabbitmq_url: str = Field(default="amqp://guest:guest@localhost:5672/")
    primary_exchange: str = Field(default="events_exchange")
    dlx_exchange: str = Field(default="dlx_exchange")
    primary_queue: str = Field(default="events_queue")
    dead_letter_queue: str = Field(default="dead_letter_queue")

    # ── DuckDB ────────────────────────────────────────────────────────────────
    duckdb_path: str = Field(default="data/events.duckdb")

    # ── Docs ──────────────────────────────────────────────────────────────────
    docs_dir: str = Field(default="docs")


settings = Settings()
