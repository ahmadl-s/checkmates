"""Entry point for the checkmates Discord bot.

Run with:  python -m checkmates
"""

from __future__ import annotations

import asyncio
import logging
import sys

from checkmates.collector import MessageCollector
from checkmates.config import Settings
from checkmates.context import ContextService
from checkmates.database import Database, MessageRepository
from checkmates.extraction.prefilter import ExtractionPrefilter
from checkmates.extraction.service import ExtractionService
from checkmates.processors.extraction import ExtractionProcessor
from checkmates.processors.persistence import PersistenceProcessor


def main() -> None:
    asyncio.run(_run_bot())


async def _run_bot() -> None:
    settings = Settings()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("checkmates")

    if not settings.discord_token:
        logger.error("DISCORD_TOKEN is not set. Add it to .env (see .env.example).")
        sys.exit(1)

    if not settings.channel_ids:
        logger.error(
            "PROJECT_CHANNEL_IDS is not set. Add channel IDs to .env (see .env.example)."
        )
        sys.exit(1)

    # Initialize database
    db = Database(settings.database_path)
    await db.init()

    extraction_processor: ExtractionProcessor | None = None

    try:
        repo = MessageRepository(db)

        # Context service for project-scoped data retrieval
        context_service = ContextService(
            repo,
            max_messages=settings.context_max_messages,
        )

        # Pre-filter with configurable minimum message length
        prefilter = ExtractionPrefilter(
            min_message_length=settings.extraction_min_message_length,
        )

        # Extraction service (Gemini-only, no OpenAI fallback)
        extraction_service = ExtractionService(
            context_service,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout=settings.gemini_timeout,
            max_concurrent=settings.gemini_max_concurrent,
            daily_request_limit=settings.gemini_daily_request_limit,
            prefilter=prefilter,
        )

        if settings.gemini_api_key:
            logger.info(
                "AI extraction ENABLED: model=%s, daily_limit=%d, "
                "max_concurrent=%d, queue_capacity=%d",
                settings.gemini_model,
                settings.gemini_daily_request_limit,
                settings.gemini_max_concurrent,
                settings.extraction_queue_capacity,
            )
        else:
            logger.warning(
                "AI extraction DISABLED: GEMINI_API_KEY is not set. "
                "Messages will be persisted but not extracted. "
                "Set GEMINI_API_KEY in .env to enable extraction."
            )

        # Wire: ExtractionProcessor wraps PersistenceProcessor.
        # Messages are persisted first, then enqueued for extraction.
        # When extraction succeeds, actionable tasks are persisted to
        # the tasks table via the repository (INSERT OR IGNORE dedup).
        persistence_processor = PersistenceProcessor(repo)
        extraction_processor = ExtractionProcessor(
            wrapped=persistence_processor,
            extraction_service=extraction_service,
            repository=repo,
            queue_capacity=settings.extraction_queue_capacity,
            shutdown_timeout=settings.extraction_shutdown_timeout,
        )

        client = MessageCollector(
            settings=settings,
            processor=extraction_processor,
            repository=repo,
        )

        logger.info(
            "Starting checkmates bot. Monitoring %d channel(s). Database: %s",
            len(settings.channel_ids),
            settings.database_path,
        )

        await client.start(settings.discord_token)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        # Drain extraction queue before closing the database.
        # The worker needs the DB (via ContextService) to resolve project
        # context for in-flight extraction requests.
        if extraction_processor is not None:
            await extraction_processor.shutdown()
        await db.close()


if __name__ == "__main__":
    main()
