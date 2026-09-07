"""Application configuration.

Settings are read from environment variables, with a fallback to a `.env`
file at the repo root (shared with docker-compose and the frontend, so
there's a single place to look for connection strings and origins).
Defaults match `docker-compose.yml` + Vite's default dev port, so the
backend runs locally with zero configuration.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[0]=app, parents[1]=backend, parents[2]=repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "analytics"
    frontend_origin: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
