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


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


from checkmates.database import Database, MessageRepository  # noqa: E402
from checkmates.models import NormalizedMessage  # noqa: E402


@pytest.fixture
async def db():
    """Create an in-memory database for testing (fast, fully isolated)."""
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> MessageRepository:
    """Repository backed by the in-memory test database."""
    return MessageRepository(db)


@pytest.fixture
def normalized_message() -> NormalizedMessage:
    """A normalized message for database tests."""
    return NormalizedMessage(
        server_id=3001,
        channel_id=2001,
        message_id=5001,
        author_id=1001,
        author_display_name="Test User",
        content="I'll finish the backend API by Wednesday.",
        timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
        edited_timestamp=None,
        is_edit=False,
    )


@pytest.fixture
def normalized_edit_message() -> NormalizedMessage:
    """An edited normalized message for database tests."""
    return NormalizedMessage(
        server_id=3001,
        channel_id=2001,
        message_id=5001,
        author_id=1001,
        author_display_name="Test User",
        content="Updated: I'll finish the backend API by Thursday.",
        timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
        edited_timestamp=datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc),
        is_edit=True,
    )


@pytest.fixture
def normalized_message_2() -> NormalizedMessage:
    """A second normalized message (different ID) for database tests."""
    return NormalizedMessage(
        server_id=3001,
        channel_id=2001,
        message_id=5002,
        author_id=1002,
        author_display_name="Another User",
        content="I'll handle the frontend by Friday.",
        timestamp=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
        edited_timestamp=None,
        is_edit=False,
    )


# ---------------------------------------------------------------------------
# Context service fixtures
# ---------------------------------------------------------------------------

from checkmates.context import ContextService  # noqa: E402


@pytest.fixture
def context_service(repo: MessageRepository) -> ContextService:
    """ContextService backed by the in-memory test database."""
    return ContextService(repo, max_messages=50)


@pytest.fixture
def context_service_small(repo: MessageRepository) -> ContextService:
    """ContextService with a small message limit for testing limits."""
    return ContextService(repo, max_messages=3)


@pytest.fixture
async def seeded_project(repo: MessageRepository) -> dict:
    """Create a project with channels, members, messages, tasks, and dependencies.

    Returns a dict mapping entity names to their IDs/values for test assertions.

    Layout:
        Project "Hackathon Bot" (server 3001)
        Channels: 2001, 2002
        Members: user 1001 ("Alice"), user 1002 ("Bob")
        Messages: 3 messages across channel 2001, timestamps 10:00, 12:00, 14:00
        Tasks: 2 tasks
        Dependency: task2 depends on task1
    """
    project = await repo.create_project(server_id=3001, name="Hackathon Bot")
    await repo.add_project_channel(project.id, 2001, 3001)
    await repo.add_project_channel(project.id, 2002, 3001)

    await repo.upsert_member(project.id, 1001, "Alice")
    await repo.upsert_member(project.id, 1002, "Bob")

    # Three messages in channel 2001, chronological timestamps
    msgs = []
    for i, (mid, ts, content) in enumerate([
        (8001, "2025-01-15T10:00:00+00:00", "I'll do the backend."),
        (8002, "2025-01-15T12:00:00+00:00", "I'll handle the frontend."),
        (8003, "2025-01-15T14:00:00+00:00", "Both done? Let me review."),
    ]):
        msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=mid,
            author_id=1001 if i < 2 else 1002,
            author_display_name="Alice" if i < 2 else "Bob",
            content=content,
            timestamp=datetime.fromisoformat(ts),
            edited_timestamp=None,
            is_edit=False,
        )
        await repo.insert_message(msg)
        msgs.append(msg)

    # One message in channel 2002
    msg_ch2 = NormalizedMessage(
        server_id=3001,
        channel_id=2002,
        message_id=8004,
        author_id=1001,
        author_display_name="Alice",
        content="Design discussion here.",
        timestamp=datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc),
        edited_timestamp=None,
        is_edit=False,
    )
    await repo.insert_message(msg_ch2)

    # Tasks
    task1 = await repo.create_task(
        project.id,
        "Backend API",
        description="Build REST endpoints",
        assignee_id=1001,
    )
    task2 = await repo.create_task(
        project.id,
        "Frontend UI",
        description="Build React components",
        assignee_id=1002,
    )

    # Dependency: task2 depends on task1
    await repo.create_dependency(task2.id, task1.id)

    return {
        "project": project,
        "channel_ids": [2001, 2002],
        "member_ids": [1001, 1002],
        "message_ids": [8001, 8002, 8003, 8004],
        "task_ids": [task1.id, task2.id],
    }
