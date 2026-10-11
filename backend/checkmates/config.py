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
        DATABASE_PATH         - SQLite database file path (default: checkmates.db)
        TIMEZONE              - Project-local timezone for deadline display (default: UTC)
        CONTEXT_MAX_MESSAGES  - Max recent messages in project context (default: 50)
        GEMINI_API_KEY        - Google Gemini API key for task extraction (free tier)
        GEMINI_MODEL          - Model for structured output extraction (default: gemini-2.5-flash-lite)
        GEMINI_TIMEOUT        - Request timeout in seconds (default: 30)
        GEMINI_MAX_CONCURRENT - Max concurrent Gemini API calls (default: 1)
        GEMINI_DAILY_REQUEST_LIMIT - App-level daily request cap (default: 50)
        EXTRACTION_MIN_MESSAGE_LENGTH - Min chars for pre-filter to consider (default: 10)
        EXTRACTION_QUEUE_CAPACITY - Max items in extraction queue (default: 50)
        EXTRACTION_SHUTDOWN_TIMEOUT - Seconds to drain extraction queue on shutdown (default: 10)
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
    database_path: str = "checkmates.db"
    timezone: str = "UTC"
    context_max_messages: int = 50
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_timeout: float = 30.0
    gemini_max_concurrent: int = 1
    gemini_daily_request_limit: int = 50
    extraction_min_message_length: int = 10
    extraction_queue_capacity: int = 50
    extraction_shutdown_timeout: float = 10.0

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
