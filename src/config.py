"""Configuration for api-core."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./api_core.db"
    api_prefix: str = "/api/v1"
    api_version: str = "2.0.0"
    debug: bool = False

    # Token pricing (per 1K tokens)
    input_token_price: float = 0.003
    output_token_price: float = 0.015
    cached_token_price: float = 0.00015

    # Devin API
    devin_api_key: str = ""
    devin_api_base: str = "https://api.devin.ai/v1"
    devin_app_base: str = "https://app.devin.ai"

    model_config = {"env_prefix": "API_CORE_"}


settings = Settings()


def calculate_cost(input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
    """Calculate token cost using configured pricing."""
    return round(
        (input_tokens / 1000) * settings.input_token_price
        + (output_tokens / 1000) * settings.output_token_price
        + (cached_tokens / 1000) * settings.cached_token_price,
        6,
    )
