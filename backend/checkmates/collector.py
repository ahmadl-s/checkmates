"""Discord message collector: listens to events, filters, normalizes, deduplicates.

This is the entry point of the collection pipeline. It is a discord.Client
subclass that wires together MessageFilter, MessageNormalizer, and
DeduplicationCache. Normalized messages are forwarded to a downstream
MessageProcessor.

Design note: The collector deliberately does NOT import the AI pipeline,
database layer, or any future feature. It only knows about the processor
interface, keeping the collection layer cleanly separated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from checkmates.config import Settings
from checkmates.dedup import DeduplicationCache
from checkmates.filtering import MessageFilter
from checkmates.normalizer import MessageNormalizer
from checkmates.processors.base import MessageProcessor

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MessageCollector(discord.Client):
    """Discord client that collects and normalizes messages from project channels.

    Lifecycle:
        on_message       -> filter -> dedup -> normalize -> processor.process()
        on_message_edit  -> filter -> dedup -> normalize(edit) -> processor.process()

    The processor is the seam where AI extraction, context management, and
    database operations will be wired in later development phases.
    """

    def __init__(
        self,
        settings: Settings,
        processor: MessageProcessor,
        *,
        intents: discord.Intents | None = None,
    ) -> None:
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.members = True  # needed for author.display_name
        super().__init__(intents=intents)

        self._settings = settings
        self._processor = processor
        self._filter = MessageFilter(settings.channel_ids)
        self._normalizer = MessageNormalizer()
        self._dedup = DeduplicationCache()

    async def on_message(self, message: discord.Message) -> None:
        """Handle new messages from Discord."""
        await self._handle_message(message, is_edit=False)

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Handle message edits — forward the edited version."""
        await self._handle_message(after, is_edit=True)

    async def _handle_message(self, message: discord.Message, *, is_edit: bool) -> None:
        """Core processing: filter, dedup, normalize, forward.

        Any exception is caught and logged so one bad message doesn't crash
        the collection loop. Message content is deliberately NOT logged.
        """
        try:
            if not self._filter.should_process(message):
                return

            if is_edit:
                if not self._dedup.should_process_edit(message.id, message.edited_at):
                    return
            else:
                if not self._dedup.should_process_original(message.id):
                    return

            normalized = self._normalizer.normalize(message, is_edit=is_edit)

            logger.info(
                "Forwarding message id=%s channel=%s author=%s edit=%s",
                normalized.message_id,
                normalized.channel_id,
                normalized.author_id,
                normalized.is_edit,
            )

            await self._processor.process(normalized)

        except Exception:
            logger.exception(
                "Error processing message id=%s channel=%s",
                getattr(message, "id", "unknown"),
                getattr(getattr(message, "channel", None), "id", "unknown"),
            )
