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
    # Comma-separated list, not a single origin (Phase 9 deployment):
    # local dev (http://localhost:5173) and a deployed frontend need to
    # both work against the same backend at once — a developer testing
    # locally against a deployed API, or just wanting local dev to keep
    # working after the frontend is deployed, shouldn't need to edit
    # .env and restart every time they switch contexts. See main.py's
    # CORS setup and README's Phase 9 design notes.
    frontend_origin: str = "http://localhost:5173"

    # Auth (role-based access control). The default below is INSECURE —
    # a fixed, publicly-known string — and exists only so the backend
    # still runs with zero configuration for local dev (same promise as
    # every other setting here). Any deployment beyond a developer's own
    # machine MUST set JWT_SECRET_KEY in the environment to something
    # random (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`),
    # or every JWT this app issues is forgeable by anyone who's read this
    # source file.
    jwt_secret_key: str = "INSECURE-DEV-ONLY-SECRET-CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24

    # On-demand anomaly explanations (POST /api/anomalies/{id}/explain,
    # see llm.py). Empty by default: the rest of the app needs no key,
    # and an unset key makes only that one endpoint answer 503 (already-
    # cached explanations still work). The model is a setting, not a
    # constant, because hosted model names get retired — swapping one
    # should be a .env edit, not a code change. That is not hypothetical:
    # the brief named gemini-2.0-flash-lite, and by the time this was
    # first run against the live API Google had retired it (a 404 "no
    # longer available"), along with the 2.5 flash models for new users.
    # gemini-3.5-flash-lite is the lite-tier replacement the API itself
    # pointed to, pinned by name (not a "-latest" alias) so a captured
    # example stays reproducible.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def frontend_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
