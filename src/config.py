"""Configuration for api-core."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./agentboard.db"
    api_prefix: str = "/api/v1"
    debug: bool = True

    # Token pricing (per 1K tokens)
    input_token_price: float = 0.003
    output_token_price: float = 0.015
    cached_token_price: float = 0.00015

    # Devin API
    devin_api_key: str = ""

    class Config:
        env_prefix = "GATEWAY_"


settings = Settings()
