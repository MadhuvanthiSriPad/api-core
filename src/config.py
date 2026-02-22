"""Configuration for api-core."""

from __future__ import annotations

try:
    from pydantic_settings import BaseSettings

    _USES_PYDANTIC_SETTINGS = True
except ModuleNotFoundError:
    # Compatibility fallback for environments where only pydantic is installed.
    # pydantic v2 exposes v1 settings under pydantic.v1; v1 exposes BaseSettings directly.
    try:
        from pydantic.v1 import BaseSettings  # type: ignore[attr-defined]
    except ImportError:
        from pydantic import BaseSettings  # type: ignore[no-redef]

    _USES_PYDANTIC_SETTINGS = False


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

    if _USES_PYDANTIC_SETTINGS:
        model_config = {"env_prefix": "API_CORE_"}
    else:
        class Config:
            env_prefix = "API_CORE_"


settings = Settings()


def calculate_cost(input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
    """Calculate token cost using configured pricing."""
    return round(
        (input_tokens / 1000) * settings.input_token_price
        + (output_tokens / 1000) * settings.output_token_price
        + (cached_tokens / 1000) * settings.cached_token_price,
        6,
    )
