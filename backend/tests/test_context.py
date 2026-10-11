"""Tests for project context management and retrieval (Task 3).

Covers:
    A. Context for a known, seeded project
    B. Project/server identification and message-to-project resolution
    C. Recent-message limits and chronological ordering
    D. Members, tasks, and task dependencies in context
    E. Empty projects and unknown/missing project context
    F. Project/server isolation
    G. Duplicate prevention in context messages
    H. Missing or incomplete optional data
    I. Context service configuration and error handling
    J. Project-channels table and repository methods
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from checkmates.context import ContextService, ProjectContext
from checkmates.database import Database, MessageRepository
from checkmates.database.models import Project
from checkmates.models import NormalizedMessage


# ---------------------------------------------------------------------------
# A. Context for a known, seeded project
# ---------------------------------------------------------------------------


class TestContextForKnownProject:
    async def test_context_returns_project(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """get_context should return the resolved project."""
        ctx = await context_service.get_context(channel_id=2001)
        assert ctx.project is not None
        assert ctx.project.id == seeded_project["project"].id
        assert ctx.project.name == "Hackathon Bot"
        assert ctx.project.server_id == 3001

    async def test_context_has_messages(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Context should include recent project messages."""
        ctx = await context_service.get_context(channel_id=2001)
        assert len(ctx.recent_messages) == 4  # 3 in ch2001 + 1 in ch2002

    async def test_context_has_members(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Context should include all project members."""
        ctx = await context_service.get_context(channel_id=2001)
        assert len(ctx.members) == 2
        member_ids = {m.discord_user_id for m in ctx.members}
        assert member_ids == {1001, 1002}

    async def test_context_has_tasks(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Context should include all project tasks."""
        ctx = await context_service.get_context(channel_id=2001)
        assert len(ctx.tasks) == 2
        task_titles = {t.title for t in ctx.tasks}
        assert task_titles == {"Backend API", "Frontend UI"}

    async def test_context_has_dependencies(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Context should include task dependencies."""
        ctx = await context_service.get_context(channel_id=2001)
        assert len(ctx.task_dependencies) == 1

    async def test_context_metadata_all_true(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Metadata flags should all be True for a fully seeded project."""
        ctx = await context_service.get_context(channel_id=2001)
        assert ctx.metadata.project_found is True
        assert ctx.metadata.has_messages is True
        assert ctx.metadata.has_tasks is True
        assert ctx.metadata.has_members is True
        assert ctx.metadata.has_dependencies is True
        assert ctx.metadata.max_messages == 50


# ---------------------------------------------------------------------------
# B. Project/server identification and message-to-project resolution
# ---------------------------------------------------------------------------


class TestProjectResolution:
    async def test_resolves_project_from_message(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """A NormalizedMessage should resolve to the correct project."""
        msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=9999,
            author_id=1001,
            author_display_name="Alice",
            content="New message",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg)
        assert ctx.project is not None
        assert ctx.project.id == seeded_project["project"].id
        assert ctx.current_message is msg

    async def test_resolves_project_from_channel_id(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """get_context should resolve project from channel_id alone."""
        ctx = await context_service.get_context(channel_id=2002)
        assert ctx.project is not None
        assert ctx.project.id == seeded_project["project"].id
        assert ctx.current_message is None

    async def test_second_channel_same_project(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Both channels of the same project should resolve to the same project."""
        ctx1 = await context_service.get_context(channel_id=2001)
        ctx2 = await context_service.get_context(channel_id=2002)
        assert ctx1.project is not None
        assert ctx2.project is not None
        assert ctx1.project.id == ctx2.project.id

    async def test_message_takes_precedence_over_channel_id(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """When both message and channel_id are given, message wins."""
        msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=9999,
            author_id=1001,
            author_display_name="Alice",
            content="Test",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg, channel_id=2002)
        # Message's channel (2001) should be used, not 2002
        assert ctx.current_message is msg
        # Still same project since both channels belong to same project
        assert ctx.project is not None


# ---------------------------------------------------------------------------
# C. Recent-message limits and chronological ordering
# ---------------------------------------------------------------------------


class TestMessageLimitsAndOrdering:
    async def test_messages_in_chronological_order(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Messages should be oldest-first (chronological ascending)."""
        ctx = await context_service.get_context(channel_id=2001)
        timestamps = [m.message_timestamp for m in ctx.recent_messages]
        assert timestamps == sorted(timestamps)

    async def test_message_limit_caps_results(
        self,
        context_service_small: ContextService,
        seeded_project: dict,
    ) -> None:
        """With max_messages=3, only 3 messages should be returned (most recent)."""
        ctx = await context_service_small.get_context(channel_id=2001)
        assert len(ctx.recent_messages) == 3
        # The most recent 3 (out of 4) should be the ones kept
        # Messages by time: 8001(10:00), 8004(11:00, ch2002), 8002(12:00), 8003(14:00)
        # Most recent 3: 8004, 8002, 8003 -> reversed to chronological: 8004, 8002, 8003
        msg_ids = {m.discord_message_id for m in ctx.recent_messages}
        assert 8001 not in msg_ids  # oldest was dropped

    async def test_limited_messages_still_chronological(
        self,
        context_service_small: ContextService,
        seeded_project: dict,
    ) -> None:
        """Even with a limit, returned messages should be chronological."""
        ctx = await context_service_small.get_context(channel_id=2001)
        timestamps = [m.message_timestamp for m in ctx.recent_messages]
        assert timestamps == sorted(timestamps)

    async def test_metadata_reflects_limit(
        self,
        context_service_small: ContextService,
        seeded_project: dict,
    ) -> None:
        """Metadata should report the configured max_messages."""
        ctx = await context_service_small.get_context(channel_id=2001)
        assert ctx.metadata.max_messages == 3

    async def test_no_messages_when_db_empty(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A project with no messages should return empty recent_messages."""
        project = await repo.create_project(server_id=3001, name="Empty Project")
        await repo.add_project_channel(project.id, 5001, 3001)

        ctx = await context_service.get_context(channel_id=5001)
        assert ctx.project is not None
        assert ctx.recent_messages == []


# ---------------------------------------------------------------------------
# D. Members, tasks, and task dependencies in context
# ---------------------------------------------------------------------------


class TestContextEntities:
    async def test_tasks_have_all_fields(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Tasks in context should have titles, descriptions, assignees, status."""
        ctx = await context_service.get_context(channel_id=2001)
        task_map = {t.title: t for t in ctx.tasks}

        assert task_map["Backend API"].description == "Build REST endpoints"
        assert task_map["Backend API"].assignee_id == 1001
        assert task_map["Backend API"].status == "pending"

        assert task_map["Frontend UI"].assignee_id == 1002

    async def test_dependencies_reference_correct_tasks(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """The dependency should reference the actual task IDs."""
        ctx = await context_service.get_context(channel_id=2001)
        dep = ctx.task_dependencies[0]
        task_ids = seeded_project["task_ids"]

        # task2 depends on task1
        assert dep.task_id == task_ids[1]
        assert dep.prerequisite_task_id == task_ids[0]

    async def test_members_have_display_names(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Members should carry their display names."""
        ctx = await context_service.get_context(channel_id=2001)
        name_map = {m.discord_user_id: m.display_name for m in ctx.members}
        assert name_map[1001] == "Alice"
        assert name_map[1002] == "Bob"


# ---------------------------------------------------------------------------
# E. Empty projects and unknown/missing project context
# ---------------------------------------------------------------------------


class TestEmptyAndUnknownContext:
    async def test_unknown_channel_returns_empty_context(
        self,
        context_service: ContextService,
    ) -> None:
        """A channel with no registered project should return empty context."""
        ctx = await context_service.get_context(channel_id=99999)
        assert ctx.project is None
        assert ctx.recent_messages == []
        assert ctx.members == []
        assert ctx.tasks == []
        assert ctx.task_dependencies == []
        assert ctx.metadata.project_found is False

    async def test_unknown_message_channel_returns_empty_context(
        self,
        context_service: ContextService,
    ) -> None:
        """A message from an unregistered channel should return empty context."""
        msg = NormalizedMessage(
            server_id=3001,
            channel_id=99999,
            message_id=12345,
            author_id=1001,
            author_display_name="Ghost",
            content="Hello",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg)
        assert ctx.project is None
        assert ctx.current_message is msg
        assert ctx.recent_messages == []

    async def test_empty_project_returns_empty_lists(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A project with channels but no messages/members/tasks should be empty."""
        project = await repo.create_project(server_id=3001, name="Empty Project")
        await repo.add_project_channel(project.id, 5001, 3001)

        ctx = await context_service.get_context(channel_id=5001)
        assert ctx.project is not None
        assert ctx.project.name == "Empty Project"
        assert ctx.recent_messages == []
        assert ctx.members == []
        assert ctx.tasks == []
        assert ctx.task_dependencies == []
        assert ctx.metadata.project_found is True
        assert ctx.metadata.has_messages is False
        assert ctx.metadata.has_members is False
        assert ctx.metadata.has_tasks is False
        assert ctx.metadata.has_dependencies is False

    async def test_context_never_invents_data(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """Context for unknown project must not fabricate any data."""
        ctx = await context_service.get_context(channel_id=88888)
        assert ctx.project is None
        assert len(ctx.recent_messages) == 0
        assert len(ctx.members) == 0
        assert len(ctx.tasks) == 0
        assert len(ctx.task_dependencies) == 0


# ---------------------------------------------------------------------------
# F. Project/server isolation
# ---------------------------------------------------------------------------


class TestProjectServerIsolation:
    async def test_messages_do_not_cross_projects(
        self,
        context_service: ContextService,
        repo: MessageRepository,
        seeded_project: dict,
    ) -> None:
        """Messages from project A must not appear in project B's context."""
        # Create a second project in a different channel
        project_b = await repo.create_project(server_id=3001, name="Project B")
        await repo.add_project_channel(project_b.id, 3001, 3001)

        # Insert a message in project B's channel
        msg_b = NormalizedMessage(
            server_id=3001,
            channel_id=3001,
            message_id=7001,
            author_id=1001,
            author_display_name="Alice",
            content="Project B message",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        await repo.insert_message(msg_b)

        # Get context for project A (channel 2001)
        ctx_a = await context_service.get_context(channel_id=2001)
        msg_ids_a = {m.discord_message_id for m in ctx_a.recent_messages}
        assert 7001 not in msg_ids_a  # Project B's message is not in A

        # Get context for project B (channel 3001)
        ctx_b = await context_service.get_context(channel_id=3001)
        msg_ids_b = {m.discord_message_id for m in ctx_b.recent_messages}
        assert 7001 in msg_ids_b
        assert 8001 not in msg_ids_b  # Project A's messages are not in B

    async def test_tasks_do_not_cross_projects(
        self,
        context_service: ContextService,
        repo: MessageRepository,
        seeded_project: dict,
    ) -> None:
        """Tasks from project A must not appear in project B's context."""
        project_b = await repo.create_project(server_id=3001, name="Project B")
        await repo.add_project_channel(project_b.id, 3001, 3001)
        await repo.create_task(project_b.id, "Project B Task")

        ctx_a = await context_service.get_context(channel_id=2001)
        task_titles_a = {t.title for t in ctx_a.tasks}
        assert "Project B Task" not in task_titles_a

        ctx_b = await context_service.get_context(channel_id=3001)
        task_titles_b = {t.title for t in ctx_b.tasks}
        assert "Project B Task" in task_titles_b
        assert "Backend API" not in task_titles_b

    async def test_members_do_not_cross_projects(
        self,
        context_service: ContextService,
        repo: MessageRepository,
        seeded_project: dict,
    ) -> None:
        """Members of project A must not appear in project B's context."""
        project_b = await repo.create_project(server_id=3001, name="Project B")
        await repo.add_project_channel(project_b.id, 3001, 3001)
        await repo.upsert_member(project_b.id, 5001, "Carol")

        ctx_a = await context_service.get_context(channel_id=2001)
        member_ids_a = {m.discord_user_id for m in ctx_a.members}
        assert 5001 not in member_ids_a  # Carol is in B, not A

        ctx_b = await context_service.get_context(channel_id=3001)
        member_ids_b = {m.discord_user_id for m in ctx_b.members}
        assert 5001 in member_ids_b
        assert 1001 not in member_ids_b  # Alice is in A, not B

    async def test_dependencies_do_not_cross_projects(
        self,
        context_service: ContextService,
        repo: MessageRepository,
        seeded_project: dict,
    ) -> None:
        """Dependencies from project A must not appear in project B's context."""
        project_b = await repo.create_project(server_id=3001, name="Project B")
        await repo.add_project_channel(project_b.id, 3001, 3001)
        t1 = await repo.create_task(project_b.id, "PB Task 1")
        t2 = await repo.create_task(project_b.id, "PB Task 2")
        await repo.create_dependency(t2.id, t1.id)

        ctx_a = await context_service.get_context(channel_id=2001)
        assert len(ctx_a.task_dependencies) == 1  # only A's dependency

        ctx_b = await context_service.get_context(channel_id=3001)
        assert len(ctx_b.task_dependencies) == 1  # only B's dependency
        # Verify they are different dependencies
        a_dep = ctx_a.task_dependencies[0]
        b_dep = ctx_b.task_dependencies[0]
        assert a_dep.task_id != b_dep.task_id

    async def test_different_servers_isolated(
        self,
        context_service: ContextService,
        repo: MessageRepository,
        seeded_project: dict,
    ) -> None:
        """Projects in different servers should be fully isolated."""
        project_other = await repo.create_project(server_id=9999, name="Other Server Project")
        await repo.add_project_channel(project_other.id, 7777, 9999)
        await repo.upsert_member(project_other.id, 8888, "Other Server User")
        await repo.create_task(project_other.id, "Other Server Task")

        msg = NormalizedMessage(
            server_id=9999,
            channel_id=7777,
            message_id=6001,
            author_id=8888,
            author_display_name="Other Server User",
            content="Different server",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        await repo.insert_message(msg)

        ctx = await context_service.get_context(channel_id=7777)
        assert ctx.project is not None
        assert ctx.project.server_id == 9999
        assert len(ctx.members) == 1
        assert ctx.members[0].discord_user_id == 8888
        assert len(ctx.recent_messages) == 1
        assert ctx.recent_messages[0].server_id == 9999


# ---------------------------------------------------------------------------
# G. Duplicate prevention in context messages
# ---------------------------------------------------------------------------


class TestDuplicatePrevention:
    async def test_no_duplicate_messages_in_context(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """Each message should appear at most once in the context."""
        ctx = await context_service.get_context(channel_id=2001)
        msg_ids = [m.discord_message_id for m in ctx.recent_messages]
        assert len(msg_ids) == len(set(msg_ids))  # no duplicates

    async def test_edited_message_appears_once(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """An edited message (same discord_message_id) should appear once."""
        project = await repo.create_project(server_id=3001, name="Edit Test")
        await repo.add_project_channel(project.id, 4001, 3001)

        # Insert original
        msg = NormalizedMessage(
            server_id=3001,
            channel_id=4001,
            message_id=9001,
            author_id=1001,
            author_display_name="Alice",
            content="Original content",
            timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
        )
        await repo.insert_message(msg)

        # Edit it
        edit = NormalizedMessage(
            server_id=3001,
            channel_id=4001,
            message_id=9001,
            author_id=1001,
            author_display_name="Alice",
            content="Edited content",
            timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
            edited_timestamp=datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc),
            is_edit=True,
        )
        await repo.update_message_edit(edit)

        ctx = await context_service.get_context(channel_id=4001)
        assert len(ctx.recent_messages) == 1
        assert ctx.recent_messages[0].content == "Edited content"


# ---------------------------------------------------------------------------
# H. Missing or incomplete optional data
# ---------------------------------------------------------------------------


class TestIncompleteData:
    async def test_task_without_assignee(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A task with no assignee should not crash context retrieval."""
        project = await repo.create_project(server_id=3001, name="No Assignee")
        await repo.add_project_channel(project.id, 4001, 3001)
        await repo.create_task(project.id, "Unassigned Task")  # no assignee_id

        ctx = await context_service.get_context(channel_id=4001)
        assert len(ctx.tasks) == 1
        assert ctx.tasks[0].assignee_id is None
        assert ctx.tasks[0].title == "Unassigned Task"

    async def test_task_without_deadline(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A task with no deadline should not crash context retrieval."""
        project = await repo.create_project(server_id=3001, name="No Deadline")
        await repo.add_project_channel(project.id, 4001, 3001)
        task = await repo.create_task(project.id, "No Deadline Task")
        assert task.deadline is None

        ctx = await context_service.get_context(channel_id=4001)
        assert ctx.tasks[0].deadline is None

    async def test_task_without_description(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A task with no description should not crash context retrieval."""
        project = await repo.create_project(server_id=3001, name="No Desc")
        await repo.add_project_channel(project.id, 4001, 3001)
        await repo.create_task(project.id, "Minimal Task")

        ctx = await context_service.get_context(channel_id=4001)
        assert ctx.tasks[0].description is None

    async def test_member_without_display_name(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """A member with no display name should not crash context retrieval."""
        project = await repo.create_project(server_id=3001, name="No Name")
        await repo.add_project_channel(project.id, 4001, 3001)
        await repo.upsert_member(project.id, 5001, None)

        ctx = await context_service.get_context(channel_id=4001)
        assert len(ctx.members) == 1
        assert ctx.members[0].display_name is None

    async def test_project_with_tasks_but_no_dependencies(
        self,
        context_service: ContextService,
        repo: MessageRepository,
    ) -> None:
        """has_tasks=True but has_dependencies=False when no deps exist."""
        project = await repo.create_project(server_id=3001, name="No Deps")
        await repo.add_project_channel(project.id, 4001, 3001)
        await repo.create_task(project.id, "Task 1")
        await repo.create_task(project.id, "Task 2")

        ctx = await context_service.get_context(channel_id=4001)
        assert ctx.metadata.has_tasks is True
        assert ctx.metadata.has_dependencies is False
        assert ctx.task_dependencies == []


# ---------------------------------------------------------------------------
# I. Context service configuration and error handling
# ---------------------------------------------------------------------------


class TestContextServiceConfig:
    def test_max_messages_property(
        self, repo: MessageRepository
    ) -> None:
        """max_messages property should reflect constructor argument."""
        svc = ContextService(repo, max_messages=10)
        assert svc.max_messages == 10

    def test_max_messages_minimum_one(
        self, repo: MessageRepository
    ) -> None:
        """max_messages should be at least 1 even if 0 or negative is passed."""
        svc = ContextService(repo, max_messages=0)
        assert svc.max_messages == 1

        svc2 = ContextService(repo, max_messages=-5)
        assert svc2.max_messages == 1

    async def test_get_context_requires_input(
        self, context_service: ContextService
    ) -> None:
        """get_context should raise if neither message nor channel_id is provided."""
        with pytest.raises(ValueError, match="requires either"):
            await context_service.get_context()

    async def test_get_context_returns_project_context_type(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """The return type should be ProjectContext."""
        ctx = await context_service.get_context(channel_id=2001)
        assert isinstance(ctx, ProjectContext)

    async def test_context_serializable(
        self,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """ProjectContext should be serializable to dict (for LLM consumption)."""
        ctx = await context_service.get_context(channel_id=2001)
        data = ctx.model_dump()
        assert "project" in data
        assert "recent_messages" in data
        assert "members" in data
        assert "tasks" in data
        assert "task_dependencies" in data
        assert "metadata" in data
        assert "current_message" in data


# ---------------------------------------------------------------------------
# J. project_channels table and repository methods
# ---------------------------------------------------------------------------


class TestProjectChannelsTable:
    async def test_add_project_channel_creates_association(
        self, repo: MessageRepository
    ) -> None:
        """add_project_channel should create a channel-project association."""
        project = await repo.create_project(server_id=3001, name="Test")
        created = await repo.add_project_channel(project.id, 5001, 3001)
        assert created is True

    async def test_add_project_channel_idempotent(
        self, repo: MessageRepository
    ) -> None:
        """Adding the same channel twice should return False the second time."""
        project = await repo.create_project(server_id=3001, name="Test")
        first = await repo.add_project_channel(project.id, 5001, 3001)
        second = await repo.add_project_channel(project.id, 5001, 3001)
        assert first is True
        assert second is False

    async def test_channel_belongs_to_one_project(
        self, repo: MessageRepository
    ) -> None:
        """A channel can only belong to one project (UNIQUE constraint)."""
        p1 = await repo.create_project(server_id=3001, name="Project A")
        p2 = await repo.create_project(server_id=3001, name="Project B")
        await repo.add_project_channel(p1.id, 5001, 3001)
        # Adding to another project should be ignored (UNIQUE on channel_id)
        result = await repo.add_project_channel(p2.id, 5001, 3001)
        assert result is False

        # Verify it still belongs to p1
        project = await repo.get_project_by_channel(5001)
        assert project is not None
        assert project.id == p1.id

    async def test_get_project_by_channel_none_for_unknown(
        self, repo: MessageRepository
    ) -> None:
        """get_project_by_channel should return None for unregistered channels."""
        result = await repo.get_project_by_channel(99999)
        assert result is None

    async def test_get_channels_by_project(
        self, repo: MessageRepository
    ) -> None:
        """get_channels_by_project should return all registered channel IDs."""
        project = await repo.create_project(server_id=3001, name="Multi-Channel")
        await repo.add_project_channel(project.id, 5001, 3001)
        await repo.add_project_channel(project.id, 5002, 3001)
        await repo.add_project_channel(project.id, 5003, 3001)

        channels = await repo.get_channels_by_project(project.id)
        assert set(channels) == {5001, 5002, 5003}

    async def test_cascade_delete_project_removes_channels(
        self, repo: MessageRepository, db: Database
    ) -> None:
        """Deleting a project should cascade-delete its project_channels."""
        project = await repo.create_project(server_id=3001, name="To Delete")
        await repo.add_project_channel(project.id, 5001, 3001)

        await db.execute("DELETE FROM projects WHERE id = ?", (project.id,))
        await db.commit()

        # Channel association should be gone
        result = await repo.get_project_by_channel(5001)
        assert result is None

    async def test_project_channels_table_exists(self, db: Database) -> None:
        """The project_channels table should be created during init."""
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_channels'"
        )
        assert len(tables) == 1

    async def test_project_channels_indexes_exist(self, db: Database) -> None:
        """Indexes on project_channels should be created."""
        indexes = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_project_channels_channel', 'idx_project_channels_project')"
        )
        assert len(indexes) == 2
