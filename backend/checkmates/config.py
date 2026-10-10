"""Environment-based configuration for the checkmates bot."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file.

    Required:
        DISCORD_TOKEN         - Discord bot token
        PROJECT_CHANNEL_IDS   - Comma-separated channel IDs to monitor

    Optional:
        LOG_LEVEL             - Logging level (default: INFO)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    discord_token: str = ""
    project_channel_ids: str = ""
    log_level: str = "INFO"

    @property
    def channel_ids(self) -> set[int]:
        """Parse the comma-separated PROJECT_CHANNEL_IDS string into a set of ints."""
        if not self.project_channel_ids.strip():
            return set()
        ids: set[int] = set()
        for raw in self.project_channel_ids.split(","):
            raw = raw.strip()
            if raw:
                try:
                    ids.add(int(raw))
                except ValueError:
                    pass
        return ids
