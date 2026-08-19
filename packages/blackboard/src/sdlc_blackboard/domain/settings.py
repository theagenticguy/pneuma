"""Environment-derived settings value object (hexagonal-arch-stack.md §1).

Lives in the domain as a pure value object; the composition root reads it from
the environment once at APP scope. ``BLACKBOARD_``-prefixed env vars populate it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BLACKBOARD_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql://blackboard:blackboard@127.0.0.1:5432/blackboard",
    )
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    #: Renderer fork for structlog (infrastructure/logging.py): pretty console for
    #: dev TTYs, one orjson line per event for production log aggregation.
    log_format: Literal["console", "json"] = "console"
    env: str = "local"

    # Pool sizing (handoff §9).
    pool_min_size: int = 2
    pool_max_size: int = 20
    pool_command_timeout: float = 30.0
