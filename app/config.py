from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables or .env file.
    Uses pydantic-settings to validate and cast configuration values.
    """
    database_url: str = "sqlite:///./inventory.db"

    # LLM Provider Configuration (Milestone 3)
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 15.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
