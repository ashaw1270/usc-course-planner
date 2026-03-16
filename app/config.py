"""Application configuration (base URL, timeouts, cache TTL)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    catalogue_base_url: str = "https://catalogue.usc.edu"
    http_timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 3600  # 1 hour


settings = Settings()
