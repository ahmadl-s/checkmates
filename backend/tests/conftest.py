"""Shared test fixtures and mock Discord objects.

These mocks are minimal dataclass-based stand-ins for discord.Message,
discord.User/Member, discord.TextChannel, and discord.Guild. They expose
only the attributes that the collection layer accesses, so we can test
filtering, normalization, and deduplication without a Discord connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class MockUser:
    """Mock of discord.User / discord.Member."""

    id: int = 1001
    bot: bool = False
    name: str = "testuser"
    display_name: str = "Test User"
    global_name: str = "TestUser"


@dataclass
class MockBotUser:
    """Mock of a Discord bot account."""

    id: int = 9999
    bot: bool = True
    name: str = "projectbot"
    display_name: str = "Project Bot"
    global_name: str = None  # type: ignore[assignment]


@dataclass
class MockChannel:
    """Mock of discord.TextChannel."""

    id: int = 2001


@dataclass
class MockGuild:
    """Mock of discord.Guild."""

    id: int = 3001


@dataclass
class MockMessage:
    """Mock of discord.Message with all attributes the collection layer reads."""

    id: int = 5001
    author: MockUser = field(default_factory=MockUser)
    channel: MockChannel = field(default_factory=MockChannel)
    guild: Optional[MockGuild] = field(default_factory=MockGuild)
    content: str = "I'll finish the backend API by Wednesday."
    created_at: datetime = field(
        default_factory=lambda: datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
    )
    edited_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


import pytest  # noqa: E402


@pytest.fixture
def configured_channel_ids() -> set[int]:
    """Channel IDs that the filter is configured to allow."""
    return {2001, 2002}


@pytest.fixture
def mock_user() -> MockUser:
    return MockUser()


@pytest.fixture
def mock_bot_user() -> MockBotUser:
    return MockBotUser()


@pytest.fixture
def mock_message() -> MockMessage:
    """A typical human message in a configured channel."""
    return MockMessage()


@pytest.fixture
def mock_bot_message(mock_bot_user: MockBotUser) -> MockMessage:
    """A message authored by a bot — should be filtered out."""
    return MockMessage(author=mock_bot_user, content="Automated status update")


@pytest.fixture
def mock_unconfigured_message() -> MockMessage:
    """A message in a channel NOT in the configured set."""
    return MockMessage(channel=MockChannel(id=9999))


@pytest.fixture
def mock_empty_message() -> MockMessage:
    """A message with empty content — should be filtered out."""
    return MockMessage(content="")


@pytest.fixture
def mock_edited_message() -> MockMessage:
    """A message that has been edited — edit timestamp is set."""
    return MockMessage(
        content="Updated: I'll finish the backend API by Thursday.",
        edited_at=datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def mock_dm_message() -> MockMessage:
    """A DM message — guild is None, server_id should be 0."""
    return MockMessage(guild=None)
