"""Application-level configuration.

Most user-facing settings live in the database (``AppSettings`` singleton).
This module covers only the bootstrap parameters: where to find data on disk,
how to start the server, and how to configure logging — things that have to
be known *before* the database connection is open.

Values come from environment variables (or a ``.env`` file in the project
root) via ``pydantic-settings``. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FactorySettings(BaseSettings):
    """Bootstrap configuration loaded from env / .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ server
    factory_host: str = Field(default="127.0.0.1")
    factory_port: int = Field(default=8765)
    factory_reload: bool = Field(default=False)

    # ----------------------------------------------------------------- logging
    factory_log_level: str = Field(default="INFO")
    factory_log_json: bool = Field(default=False)
    factory_log_rich: bool = Field(default=True)
    factory_log_retain_days: int = Field(default=14)

    # -------------------------------------------------------------------- data
    factory_data_dir: Path | None = Field(default=None)

    # ----------------------------------------------------------- embedding
    # Set if Robomuffin is being consumed by another local app (e.g. the
    # WP/WooCommerce admin tool described in INTEGRATION.md). When non-empty,
    # every /api/* request must carry header `X-Robomuffin-Key: <value>`.
    # Leave empty to disable auth (default — assumes you're on a trusted
    # local network).
    factory_api_key: str = Field(default="")
    # Comma-separated origins allowed by the CORS middleware. The default
    # covers the typical "local app on a non-default port" case. Add the
    # actual origin of the embedding app if it lives on a different port.
    factory_cors_origins: str = Field(
        default="http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173,http://127.0.0.1:5173"
    )

    # ----------------------------------------------------------- seed defaults
    # Values used only to *seed* AppSettings on first DB creation. After that
    # the DB row is authoritative and edits happen through the Settings UI.
    comfyui_urls: str = Field(default="http://localhost:8188")

    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-4o")
    anthropic_api_key: str = Field(default="")
    anthropic_model: str = Field(default="claude-sonnet-4-5")
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-2.0-flash")
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.1:8b")
    default_llm_provider: str = Field(default="")

    default_image_width: int = Field(default=1024)
    default_image_height: int = Field(default=1024)
    default_seed_mode: str = Field(default="random")
    max_parallel_per_worker: int = Field(default=1)
    restrict_explicit_content: bool = Field(default=False)

    # ------------------------------------------------------------------ paths
    @property
    def data_dir(self) -> Path:
        return self.factory_data_dir or (PROJECT_ROOT / "data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "factory.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def llm_logs_dir(self) -> Path:
        return self.logs_dir / "llm"

    @property
    def app_log_file(self) -> Path:
        return self.logs_dir / "factory.log"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def static_dir(self) -> Path:
        """Location of the bundled UI static assets (mounted at /static)."""
        return PROJECT_ROOT / "app" / "web" / "static"

    @property
    def templates_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "web" / "templates"

    def ensure_dirs(self) -> None:
        """Create every directory required for the app to operate."""
        for p in (self.data_dir, self.logs_dir, self.llm_logs_dir, self.projects_dir):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse FACTORY_CORS_ORIGINS into a list of origin URLs."""
        return [o.strip() for o in (self.factory_cors_origins or "").split(",") if o.strip()]

    # --------------------------------------------------------------- list helpers
    def comfyui_urls_list(self) -> list[str]:
        """Parse the comma-separated COMFYUI_URLS env into a clean list."""
        return [u.strip().rstrip("/") for u in self.comfyui_urls.split(",") if u.strip()]


@lru_cache(maxsize=1)
def get_settings() -> FactorySettings:
    """Return the cached, process-wide settings instance."""
    return FactorySettings()
