"""Focused tests for the !tasks Discord command.

Tests:
1. Project isolation — !tasks in channel A shows only project A's tasks.
2. Empty results — no tasks -> friendly message.
3. No project mapped — unmapped channel -> friendly message.
4. Task formatting — title, status, assignee, deadline all present.
5. Command not forwarded to processor — !tasks never reaches the pipeline.
6. Message splitting — many tasks produce multiple Discord messages.
7. Backward compat — no repository -> !tasks falls through to processor.
8. Bot messages ignored — bot's own !tasks is not intercepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import discord
import pytest

from checkmates.collector import MessageCollector, _split_message
from checkmates.config import Settings
from checkmates.database import Database, MessageRepository
from checkmates.models import NormalizedMessage
from tests.conftest import MockUser, MockGuild


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


@dataclass
class SendRecordingChannel:
    """Mock channel that records all sent messages."""

    id: int = 2001
    sent_messages: list[str] = field(default_factory=list)

    async def send(self, content: str = "", **kwargs) -> None:
        self.sent_messages.append(content)


@dataclass
class CommandMockMessage:
    """Mock message for !tasks command tests."""

    id: int = 6001
    author: MockUser = field(default_factory=MockUser)
    channel: SendRecordingChannel = field(default_factory=SendRecordingChannel)
    guild: Optional[MockGuild] = field(default_factory=MockGuild)
    content: str = "!tasks"
    created_at: datetime = field(
        default_factory=lambda: datetime(2025, 1, 15, 16, 0, tzinfo=timezone.utc)
    )
    edited_at: Optional[datetime] = None


class RecordingProcessor:
    """Mock processor that records processed messages."""

    def __init__(self) -> None:
        self.processed: list[NormalizedMessage] = []

    async def process(self, message: NormalizedMessage) -> None:
        self.processed.append(message)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_settings() -> Settings:
    """Settings with channels 2001 and 2002 configured."""
    return Settings(
        discord_token="test_token",
        project_channel_ids="2001,2002",
        database_path=":memory:",
    )


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> MessageRepository:
    return MessageRepository(db)


@pytest.fixture
def intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return intents


@pytest.fixture
async def two_projects(repo: MessageRepository) -> dict:
    """Create two projects with channels, members, and tasks.

    Layout:
        Project A "Alpha" (server 3001)
            Channels: 2001
            Members: 1001 "Alice", 1002 "Bob"
            Tasks: 2 tasks

        Project B "Beta" (server 3001)
            Channels: 2002
            Members: 1003 "Carol"
            Tasks: 2 tasks
    """
    proj_a = await repo.create_project(server_id=3001, name="Alpha")
    await repo.add_project_channel(proj_a.id, 2001, 3001)
    await repo.upsert_member(proj_a.id, 1001, "Alice")
    await repo.upsert_member(proj_a.id, 1002, "Bob")
    await repo.create_task(
        proj_a.id, "Build API", description="REST endpoints",
        assignee_id=1001,
        deadline=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
    )
    await repo.create_task(
        proj_a.id, "Write tests", assignee_id=1002,
    )

    proj_b = await repo.create_project(server_id=3001, name="Beta")
    await repo.add_project_channel(proj_b.id, 2002, 3001)
    await repo.upsert_member(proj_b.id, 1003, "Carol")
    await repo.create_task(
        proj_b.id, "Design UI", assignee_id=1003,
        deadline=datetime(2025, 1, 18, 10, 0, tzinfo=timezone.utc),
    )
    await repo.create_task(
        proj_b.id, "Deploy app",
    )

    return {
        "project_a": proj_a,
        "project_b": proj_b,
        "channel_a": 2001,
        "channel_b": 2002,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_tasks_command(
    settings: Settings,
    repo: MessageRepository,
    intents: discord.Intents,
    channel_id: int,
) -> SendRecordingChannel:
    """Create a MessageCollector, send !tasks in the given channel, return the channel mock."""
    processor = RecordingProcessor()
    client = MessageCollector(
        settings=settings,
        processor=processor,
        repository=repo,
        intents=intents,
    )

    channel = SendRecordingChannel(id=channel_id)
    msg = CommandMockMessage(
        content="!tasks",
        channel=channel,
        author=MockUser(id=1001, display_name="Test User"),
        guild=MockGuild(id=3001),
    )

    await client.on_message(msg)
    await client.close()
    return channel


# ---------------------------------------------------------------------------
# 1. Project isolation
# ---------------------------------------------------------------------------


class TestProjectIsolation:
    async def test_channel_a_shows_only_project_a_tasks(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """!tasks in channel 2001 must show only Alpha's tasks, not Beta's."""
        channel = await _send_tasks_command(
            test_settings, repo, intents, two_projects["channel_a"]
        )

        assert len(channel.sent_messages) >= 1
        combined = "\n".join(channel.sent_messages)

        # Alpha's tasks should appear
        assert "Build API" in combined
        assert "Write tests" in combined
        assert "Alpha" in combined

        # Beta's tasks must NOT appear
        assert "Design UI" not in combined
        assert "Deploy app" not in combined
        assert "Beta" not in combined

    async def test_channel_b_shows_only_project_b_tasks(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """!tasks in channel 2002 must show only Beta's tasks, not Alpha's."""
        channel = await _send_tasks_command(
            test_settings, repo, intents, two_projects["channel_b"]
        )

        assert len(channel.sent_messages) >= 1
        combined = "\n".join(channel.sent_messages)

        # Beta's tasks should appear
        assert "Design UI" in combined
        assert "Deploy app" in combined
        assert "Beta" in combined

        # Alpha's tasks must NOT appear
        assert "Build API" not in combined
        assert "Write tests" not in combined
        assert "Alpha" not in combined


# ---------------------------------------------------------------------------
# 2. Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    async def test_no_tasks_friendly_message(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
    ) -> None:
        """Project with no tasks -> friendly 'no tasks yet' message."""
        project = await repo.create_project(server_id=3001, name="Empty Project")
        await repo.add_project_channel(project.id, 2003, 3001)

        channel = await _send_tasks_command(
            test_settings, repo, intents, 2003
        )

        assert len(channel.sent_messages) == 1
        msg = channel.sent_messages[0]
        assert "No tasks" in msg
        assert "Empty Project" in msg


# ---------------------------------------------------------------------------
# 3. No project mapped
# ---------------------------------------------------------------------------


class TestNoProjectMapped:
    async def test_unmapped_channel_friendly_message(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
    ) -> None:
        """Channel with no project mapping -> friendly 'no project' message."""
        channel = await _send_tasks_command(
            test_settings, repo, intents, 9999
        )

        assert len(channel.sent_messages) == 1
        msg = channel.sent_messages[0]
        assert "No project" in msg


# ---------------------------------------------------------------------------
# 4. Task formatting
# ---------------------------------------------------------------------------


class TestTaskFormatting:
    async def test_title_status_assignee_deadline_shown(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """Output must include title, status, assignee name, and deadline."""
        channel = await _send_tasks_command(
            test_settings, repo, intents, two_projects["channel_a"]
        )

        combined = "\n".join(channel.sent_messages)

        # Title
        assert "Build API" in combined
        assert "Write tests" in combined

        # Status (both are 'pending' by default)
        assert "[pending]" in combined

        # Assignee names resolved from members table
        assert "Alice" in combined
        assert "Bob" in combined

        # Deadlines (stored as ISO datetime strings by create_task)
        assert "2025-01-15" in combined
        assert "Not set" in combined  # second task has no deadline

    async def test_unassigned_task_shows_unassigned(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """Task with no assignee shows 'Unassigned'."""
        channel = await _send_tasks_command(
            test_settings, repo, intents, two_projects["channel_b"]
        )

        combined = "\n".join(channel.sent_messages)
        assert "Unassigned" in combined  # "Deploy app" has no assignee


# ---------------------------------------------------------------------------
# 5. Command not forwarded to processor
# ---------------------------------------------------------------------------


class TestCommandNotProcessed:
    async def test_tasks_command_not_forwarded(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """!tasks message must not reach the collection processor."""
        processor = RecordingProcessor()
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            repository=repo,
            intents=intents,
        )

        msg = CommandMockMessage(
            content="!tasks",
            channel=SendRecordingChannel(id=2001),
            guild=MockGuild(id=3001),
        )
        await client.on_message(msg)
        await client.close()

        assert len(processor.processed) == 0


# ---------------------------------------------------------------------------
# 6. Message splitting
# ---------------------------------------------------------------------------


class TestMessageSplitting:
    async def test_many_tasks_split_into_multiple_messages(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
    ) -> None:
        """When output exceeds 2000 chars, it is split into multiple sends."""
        project = await repo.create_project(server_id=3001, name="Big Project")
        await repo.add_project_channel(project.id, 2001, 3001)
        await repo.upsert_member(project.id, 1001, "Alice")

        # Create enough tasks to exceed 2000 chars (each line ~100 chars)
        for i in range(30):
            await repo.create_task(
                project.id,
                f"Task {i}: This is a sufficiently long task title to fill space",
                assignee_id=1001,
                deadline=datetime(2025, 1, 20, 10, 0, tzinfo=timezone.utc),
            )

        channel = await _send_tasks_command(
            test_settings, repo, intents, 2001
        )

        # Must have sent multiple messages
        assert len(channel.sent_messages) > 1

        # Each chunk must be within Discord's limit
        for chunk in channel.sent_messages:
            assert len(chunk) <= 2000

        # All 30 tasks should be present across all chunks
        combined = "\n".join(channel.sent_messages)
        for i in range(30):
            assert f"Task {i}:" in combined

    def test_split_message_short_text(self) -> None:
        """Short text returns a single chunk."""
        result = _split_message("Hello world", max_len=2000)
        assert len(result) == 1
        assert result[0] == "Hello world"

    def test_split_message_exact_limit(self) -> None:
        """Text exactly at the limit stays in one chunk."""
        text = "a" * 2000
        result = _split_message(text, max_len=2000)
        assert len(result) == 1

    def test_split_message_over_limit(self) -> None:
        """Text over the limit is split on newlines."""
        line = "x" * 100
        text = "\n".join([line] * 30)  # 3000+ chars
        result = _split_message(text, max_len=2000)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 2000

    def test_split_message_single_long_line(self) -> None:
        """A single line exceeding the limit is split at word boundaries."""
        words = ["word"] * 500  # ~2000 chars
        text = " ".join(words)
        result = _split_message(text, max_len=2000)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 2000


# ---------------------------------------------------------------------------
# 7. Backward compatibility — no repository
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    async def test_no_repository_falls_through_to_processor(
        self,
        test_settings: Settings,
        intents: discord.Intents,
    ) -> None:
        """Without a repository, !tasks is treated as a normal message."""
        processor = RecordingProcessor()
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            repository=None,
            intents=intents,
        )

        msg = CommandMockMessage(
            content="!tasks",
            channel=SendRecordingChannel(id=2001),
            guild=MockGuild(id=3001),
        )
        await client.on_message(msg)
        await client.close()

        # The message should have been forwarded to the processor
        assert len(processor.processed) == 1
        assert processor.processed[0].content == "!tasks"


# ---------------------------------------------------------------------------
# 8. Bot messages ignored
# ---------------------------------------------------------------------------


class TestBotMessagesIgnored:
    async def test_bot_author_not_intercepted(
        self,
        test_settings: Settings,
        repo: MessageRepository,
        intents: discord.Intents,
        two_projects: dict,
    ) -> None:
        """A bot user sending !tasks should not trigger the command handler."""
        from tests.conftest import MockBotUser

        processor = RecordingProcessor()
        client = MessageCollector(
            settings=test_settings,
            processor=processor,
            repository=repo,
            intents=intents,
        )

        channel = SendRecordingChannel(id=2001)
        msg = CommandMockMessage(
            content="!tasks",
            channel=channel,
            author=MockBotUser(),
            guild=MockGuild(id=3001),
        )
        await client.on_message(msg)
        await client.close()

        # Bot messages are filtered out — no command response, no processing
        assert len(channel.sent_messages) == 0
        assert len(processor.processed) == 0
