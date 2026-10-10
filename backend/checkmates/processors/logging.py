"""LoggingProcessor: default no-op processor that logs collected messages.

This is a placeholder until the AI extraction pipeline is implemented.
It demonstrates the MessageProcessor interface and provides visibility
into the collection layer during development and debugging.

Message content is NOT logged to protect user privacy.
"""

from __future__ import annotations

import logging

from checkmates.models import NormalizedMessage

logger = logging.getLogger(__name__)


class LoggingProcessor:
    """Default processor that logs message metadata without content."""

    async def process(self, message: NormalizedMessage) -> None:
        logger.info(
            "Collected: msg_id=%s server=%s channel=%s author=%s edit=%s ts=%s",
            message.message_id,
            message.server_id,
            message.channel_id,
            message.author_id,
            message.is_edit,
            message.timestamp.isoformat(),
        )
