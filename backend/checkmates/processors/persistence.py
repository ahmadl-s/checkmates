"""PersistenceProcessor: persists collected messages to the database.

This replaces LoggingProcessor as the primary processor. It implements the
MessageProcessor protocol and delegates to MessageRepository for all DB
operations. The repository handles idempotency via UNIQUE constraints.

Flow:
    NormalizedMessage → upsert_message() → DB row created or updated
"""

from __future__ import annotations

import logging

from checkmates.database.repository import MessageRepository
from checkmates.models import NormalizedMessage

logger = logging.getLogger(__name__)


class PersistenceProcessor:
    """Processor that persists normalized messages to the database.

    For original messages: inserts (idempotent — duplicates ignored by DB).
    For edited messages: updates the existing row, or inserts if missing.
    """

    def __init__(self, repository: MessageRepository) -> None:
        self._repo = repository

    async def process(self, message: NormalizedMessage) -> None:
        """Persist the message to the database.

        Raises on database errors so the collector can handle retry logic.
        """
        inserted = await self._repo.upsert_message(message)
        action = "inserted" if inserted else "updated/idempotent"
        logger.info(
            "Persisted message discord_id=%s action=%s edit=%s",
            message.message_id,
            action,
            message.is_edit,
        )
