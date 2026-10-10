"""Entry point for the checkmates Discord bot.

Run with:  python -m checkmates
"""

from __future__ import annotations

import logging
import sys

from checkmates.collector import MessageCollector
from checkmates.config import Settings
from checkmates.processors.logging import LoggingProcessor


def main() -> None:
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

    logger.info(
        "Starting checkmates bot. Monitoring %d channel(s).",
        len(settings.channel_ids),
    )

    processor = LoggingProcessor()
    client = MessageCollector(settings=settings, processor=processor)
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
