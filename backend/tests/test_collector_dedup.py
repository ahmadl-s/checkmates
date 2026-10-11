"""Tests for collector dedup ordering and regression of original collection behavior.

The critical fix in Task 2: the in-memory dedup cache is now checked BEFORE
processing but marked ONLY AFTER successful processing. If processor.process()
raises, the message is NOT marked and can be retried on the next event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import pytest

from checkmates.config import Settings
from checkmates.dedup import DeduplicationCache
from checkmates.models import NormalizedMessage

from tests.conftest import MockMessage, MockUser, MockChannel, MockGuild


# ---------------------------------------------------------------------------
# A. DeduplicationCache split-method semantics
# ---------------------------------------------------------------------------


class TestDedupSplitMethods:
    def test_is_seen_original_returns_false_before_marking(self) -> None:
        cache = DeduplicationCache()
        assert cache.is_seen_original(5001) is False

    def test_is_seen_original_returns_true_after_marking(self) -> None:
        cache = DeduplicationCache()
        cache.mark_original(5001)
        assert cache.is_seen_original(5001) is True

    def test_mark_original_does_not_affect_edits(self) -> None:
        cache = DeduplicationCache()
        cache.mark_original(5001)
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        assert cache.is_seen_edit(5001, ts) is False

    def test_is_seen_edit_returns_false_before_marking(self) -> None:
        cache = DeduplicationCache()
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        assert cache.is_seen_edit(5001, ts) is False

    def test_is_seen_edit_returns_true_after_marking(self) -> None:
        cache = DeduplicationCache()
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        cache.mark_edit(5001, ts)
        assert cache.is_seen_edit(5001, ts) is True

    def test_mark_edit_does_not_affect_originals(self) -> None:
        cache = DeduplicationCache()
        ts = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        cache.mark_edit(5001, ts)
        assert cache.is_seen_original(5001) is False

    def test_backward_compat_should_process_original(self) -> None:
        """Old combined check-and-mark method still works."""
        cache = DeduplicationCache()
        assert cache.should_process_original(5001) is True
        assert cache.should_process_original(5001) is False


# ---------------------------------------------------------------------------
# B. Collector dedup ordering with mock processor
# ---------------------------------------------------------------------------


class RecordingProcessor:
    """Mock processor that records calls and optionally raises."""

    def __init__(self, *, should_raise: bool = False) -> None:
        self.should_raise = should_raise
        self.processed: list[NormalizedMessage] = []

    async def process(self, message: NormalizedMessage) -> None:
        if self.should_raise:
            raise RuntimeError("Simulated processing failure")
        self.processed.append(message)


@pytest.fixture
def test_settings() -> Settings:
    """Settings with channel 2001 configured."""
    return Settings(
        discord_token="test_token",
        project_channel_ids="2001",
        database_path=":memory:",
    )


@pytest.fixture
def mock_discord_message() -> MockMessage:
    return MockMessage(
        id=7001,
        author=MockUser(id=1001, display_name="Test User"),
        channel=MockChannel(id=2001),
        guild=MockGuild(id=3001),
        content="I'll write tests for the database layer.",
        created_at=datetime(2025, 1, 15, 15, 0, tzinfo=timezone.utc),
    )


class TestCollectorDedupOrdering:
    async def test_message_marked_after_successful_processing(
        self,
        test_settings: Settings,
        mock_discord_message: MockMessage,
    ) -> None:
        """If processing succeeds, the dedup cache should be marked."""
        import discord

        from checkmates.collector import MessageCollector

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        processor = RecordingProcessor(should_raise=False)
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            intents=intents,
        )

        # Process the same message twice
        await client._handle_message(mock_discord_message, is_edit=False)
        await client._handle_message(mock_discord_message, is_edit=False)

        # First call should have processed; second should be deduped
        assert len(processor.processed) == 1

        # Clean up
        await client.close()

    async def test_message_not_marked_when_processor_fails(
        self,
        test_settings: Settings,
        mock_discord_message: MockMessage,
    ) -> None:
        """If processing fails, the dedup cache should NOT be marked (allows retry)."""
        import discord

        from checkmates.collector import MessageCollector

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        processor = RecordingProcessor(should_raise=True)
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            intents=intents,
        )

        # First attempt: processor raises — message should NOT be marked
        await client._handle_message(mock_discord_message, is_edit=False)
        assert len(processor.processed) == 0  # nothing successfully processed

        # Switch processor to succeed mode
        processor.should_raise = False

        # Second attempt: should process the message again (not deduped)
        await client._handle_message(mock_discord_message, is_edit=False)
        assert len(processor.processed) == 1  # now it was processed

        # Third attempt: should be deduped (marked after success)
        await client._handle_message(mock_discord_message, is_edit=False)
        assert len(processor.processed) == 1  # not processed again

        await client.close()

    async def test_filtered_message_not_processed(
        self,
        test_settings: Settings,
        mock_discord_message: MockMessage,
    ) -> None:
        """Messages from unconfigured channels should not reach the processor."""
        import discord

        from checkmates.collector import MessageCollector

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        processor = RecordingProcessor(should_raise=False)
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            intents=intents,
        )

        # Message from an unconfigured channel (9999)
        unconfigured_msg = MockMessage(channel=MockChannel(id=9999))
        await client._handle_message(unconfigured_msg, is_edit=False)

        assert len(processor.processed) == 0

        await client.close()
