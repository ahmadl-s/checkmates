"""Tests for PersistenceProcessor: integration with MessageRepository.

Also tests the processor as a drop-in replacement for LoggingProcessor
(it satisfies the MessageProcessor protocol).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from checkmates.database import Database, MessageRepository
from checkmates.models import NormalizedMessage
from checkmates.processors.base import MessageProcessor
from checkmates.processors.persistence import PersistenceProcessor

from tests.conftest import MockMessage


@pytest.fixture
def mock_discord_message():
    """A mock Discord message for collector-level tests."""
    from tests.conftest import MockUser, MockChannel, MockGuild

    return MockMessage(
        id=6001,
        author=MockUser(id=1001, display_name="Dev User"),
        channel=MockChannel(id=2001),
        guild=MockGuild(id=3001),
        content="I'll write the database tests by Friday.",
        created_at=datetime(2025, 1, 15, 14, 0, tzinfo=timezone.utc),
    )


class TestPersistenceProcessorProtocol:
    def test_satisfies_message_processor_protocol(
        self, repo: MessageRepository
    ) -> None:
        """PersistenceProcessor must satisfy the MessageProcessor protocol."""
        processor = PersistenceProcessor(repo)
        assert isinstance(processor, MessageProcessor)


class TestPersistOriginalMessage:
    async def test_persist_original_message(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
    ) -> None:
        processor = PersistenceProcessor(repo)
        await processor.process(normalized_message)

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved is not None
        assert retrieved.content == "I'll finish the backend API by Wednesday."
        assert retrieved.processed_at is not None

    async def test_persist_duplicate_is_idempotent(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
    ) -> None:
        processor = PersistenceProcessor(repo)

        # Process the same message twice — should not error
        await processor.process(normalized_message)
        await processor.process(normalized_message)

        messages = await repo.get_messages_by_channel(2001)
        assert len(messages) == 1  # no duplicate rows


class TestPersistEditedMessage:
    async def test_persist_edit_updates_content(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
        normalized_edit_message: NormalizedMessage,
    ) -> None:
        processor = PersistenceProcessor(repo)

        # First: original message
        await processor.process(normalized_message)

        # Second: edited version
        await processor.process(normalized_edit_message)

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved.content == "Updated: I'll finish the backend API by Thursday."
        assert retrieved.edited_timestamp is not None

    async def test_persist_edit_without_original_inserts_new(
        self,
        repo: MessageRepository,
        normalized_edit_message: NormalizedMessage,
    ) -> None:
        """Edge case: edit arrives before original — should insert gracefully."""
        processor = PersistenceProcessor(repo)
        await processor.process(normalized_edit_message)

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved is not None
        assert retrieved.content == "Updated: I'll finish the backend API by Thursday."


class TestPersistenceFailure:
    async def test_processor_raises_on_db_failure(
        self,
        normalized_message: NormalizedMessage,
    ) -> None:
        """When the database is closed, processor.process() should raise.

        This is the critical retry behavior: if persistence fails, the
        exception propagates so the collector does NOT mark the in-memory
        dedup cache, allowing the message to be retried.
        """
        db = Database(":memory:")
        await db.init()
        repo = MessageRepository(db)
        processor = PersistenceProcessor(repo)

        # Close the DB to simulate a transient failure
        await db.close()

        with pytest.raises(Exception):
            await processor.process(normalized_message)
