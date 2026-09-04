"""Application configuration, loaded from the environment (and .env in development).

Every path is resolved to an absolute path against the repo root at startup. That's what
lets `BOOKS_ROOT=data/books` mean the same thing on this Mac and on the Linux VPS without
anyone editing a config file — and it's what the download endpoint will later compare
against to prove a resolved file actually lives inside the library.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str

    books_root: Path = Path("data/books")
    covers_root: Path = Path("data/covers")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"

    # Guards the /admin control panel (HTTP Basic). Required, no default -- an admin
    # panel with a guessable or absent credential is worse than no panel at all.
    admin_username: str
    admin_password: str

    @field_validator("books_root", "covers_root")
    @classmethod
    def _resolve_against_repo_root(cls, value: Path) -> Path:
        """Relative paths are interpreted relative to the repo, not the working directory —
        so the API behaves identically whether it's started from the repo root, from a
        systemd unit, or from inside a container."""
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()

    @property
    def alembic_database_url(self) -> str:
        """Alembic runs its own async engine; this exists so the sync driver form is a
        single obvious place to change if that ever stops being true."""
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
