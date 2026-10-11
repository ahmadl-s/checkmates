"""Regression tests for context isolation and data integrity (Task 3.1).

These tests verify that project/server boundaries are enforced even when
the database contains incorrectly associated records or invalid relationships.

Covers:
    A. Channel registration with mismatched server_id
    B. Context retrieval for a message whose server_id conflicts with the
       registered channel mapping
    C. Cross-server message leakage prevention
    D. Cross-project task dependency prevention (insertion + query)
    E. Normal same-project context retrieval still works (regression guard)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiosqlite import IntegrityError

from checkmates.context import ContextService
from checkmates.database import Database, MessageRepository
from checkmates.models import NormalizedMessage


# ---------------------------------------------------------------------------
# A. Channel registration with mismatched server_id
# ---------------------------------------------------------------------------


class TestChannelRegistrationServerValidation:
    async def test_register_channel_with_mismatched_server_id_raises(
        self, repo: MessageRepository
    ) -> None:
        """add_project_channel must reject a channel whose server_id
        does not match the project's server_id."""
        project = await repo.create_project(server_id=3001, name="Project A")

        with pytest.raises(ValueError, match="Server ID mismatch"):
            await repo.add_project_channel(project.id, 5001, 9999)

    async def test_register_channel_with_matching_server_id_succeeds(
        self, repo: MessageRepository
    ) -> None:
        """add_project_channel should succeed when server_id matches."""
        project = await repo.create_project(server_id=3001, name="Project A")
        created = await repo.add_project_channel(project.id, 5001, 3001)
        assert created is True

    async def test_register_channel_for_nonexistent_project_raises(
        self, repo: MessageRepository
    ) -> None:
        """add_project_channel must reject a non-existent project_id."""
        with pytest.raises(ValueError, match="does not exist"):
            await repo.add_project_channel(99999, 5001, 3001)

    async def test_existing_valid_channel_registration_still_works(
        self, repo: MessageRepository
    ) -> None:
        """Idempotent re-registration of a valid channel should return False."""
        project = await repo.create_project(server_id=3001, name="Project A")
        first = await repo.add_project_channel(project.id, 5001, 3001)
        second = await repo.add_project_channel(project.id, 5001, 3001)
        assert first is True
        assert second is False


# ---------------------------------------------------------------------------
# B. Context retrieval with conflicting server_id
# ---------------------------------------------------------------------------


class TestContextRetrievalServerValidation:
    async def test_message_with_mismatched_server_returns_empty_context(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """A message whose server_id doesn't match the project's server_id
        must not retrieve the project's context."""
        project = await repo.create_project(server_id=3001, name="Project A")
        await repo.add_project_channel(project.id, 2001, 3001)

        # Insert some data in the project
        await repo.upsert_member(project.id, 1001, "Alice")
        await repo.create_task(project.id, "Task 1")

        # Message claims to be from a different server but same channel
        msg = NormalizedMessage(
            server_id=9999,
            channel_id=2001,
            message_id=7001,
            author_id=1001,
            author_display_name="Alice",
            content="Sneaky message",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg)

        assert ctx.project is None
        assert ctx.current_message is msg
        assert ctx.recent_messages == []
        assert ctx.members == []
        assert ctx.tasks == []
        assert ctx.task_dependencies == []
        assert ctx.metadata.project_found is False

    async def test_message_with_matching_server_returns_full_context(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """A message whose server_id matches should return full context."""
        project = await repo.create_project(server_id=3001, name="Project A")
        await repo.add_project_channel(project.id, 2001, 3001)
        await repo.upsert_member(project.id, 1001, "Alice")

        msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=7001,
            author_id=1001,
            author_display_name="Alice",
            content="Valid message",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg)

        assert ctx.project is not None
        assert ctx.project.server_id == 3001
        assert ctx.current_message is msg
        assert len(ctx.members) == 1


# ---------------------------------------------------------------------------
# C. Cross-server message leakage prevention
# ---------------------------------------------------------------------------


class TestCrossServerMessageLeakage:
    async def test_mismatched_server_message_excluded_from_project_context(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """A message with a mismatched server_id must not appear in the
        project's recent_messages when retrieved via get_messages_by_project."""
        project = await repo.create_project(server_id=3001, name="Project A")
        await repo.add_project_channel(project.id, 2001, 3001)

        # Insert a valid message (matching server_id)
        valid_msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=8001,
            author_id=1001,
            author_display_name="Alice",
            content="Valid message",
            timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
        )
        await repo.insert_message(valid_msg)

        # Insert a message with mismatched server_id (manually, same channel_id)
        bad_msg = NormalizedMessage(
            server_id=9999,
            channel_id=2001,
            message_id=8002,
            author_id=1001,
            author_display_name="Alice",
            content="Sneaky cross-server message",
            timestamp=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
        )
        await repo.insert_message(bad_msg)

        # get_messages_by_project should only return the valid message
        messages = await repo.get_messages_by_project(project.id, limit=50)
        msg_ids = {m.discord_message_id for m in messages}
        assert 8001 in msg_ids
        assert 8002 not in msg_ids  # excluded due to server_id mismatch

    async def test_context_does_not_include_cross_server_messages(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """Full context retrieval must not include cross-server messages."""
        project = await repo.create_project(server_id=3001, name="Project A")
        await repo.add_project_channel(project.id, 2001, 3001)

        # Valid message
        await repo.insert_message(
            NormalizedMessage(
                server_id=3001,
                channel_id=2001,
                message_id=8001,
                author_id=1001,
                author_display_name="Alice",
                content="Valid",
                timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
            )
        )
        # Mismatched server message
        await repo.insert_message(
            NormalizedMessage(
                server_id=9999,
                channel_id=2001,
                message_id=8002,
                author_id=1001,
                author_display_name="Alice",
                content="Invalid",
                timestamp=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
            )
        )

        ctx = await context_service.get_context(channel_id=2001)
        msg_ids = {m.discord_message_id for m in ctx.recent_messages}
        assert 8001 in msg_ids
        assert 8002 not in msg_ids


# ---------------------------------------------------------------------------
# D. Cross-project task dependency prevention
# ---------------------------------------------------------------------------


class TestCrossProjectDependencyPrevention:
    async def test_create_cross_project_dependency_raises(
        self, repo: MessageRepository
    ) -> None:
        """create_dependency must reject tasks from different projects."""
        project_a = await repo.create_project(server_id=3001, name="Project A")
        project_b = await repo.create_project(server_id=3001, name="Project B")

        task_a = await repo.create_task(project_a.id, "Task A")
        task_b = await repo.create_task(project_b.id, "Task B")

        with pytest.raises(ValueError, match="Cross-project dependency rejected"):
            await repo.create_dependency(task_a.id, task_b.id)

    async def test_create_dependency_nonexistent_task_raises(
        self, repo: MessageRepository
    ) -> None:
        """create_dependency must reject non-existent tasks."""
        project = await repo.create_project(server_id=3001, name="Project A")
        task = await repo.create_task(project.id, "Task 1")

        with pytest.raises(ValueError, match="does not exist"):
            await repo.create_dependency(task.id, 99999)

        with pytest.raises(ValueError, match="does not exist"):
            await repo.create_dependency(99999, task.id)

    async def test_same_project_dependency_still_works(
        self, repo: MessageRepository
    ) -> None:
        """Dependencies within the same project should still be created."""
        project = await repo.create_project(server_id=3001, name="Project A")
        t1 = await repo.create_task(project.id, "Task 1")
        t2 = await repo.create_task(project.id, "Task 2")

        created = await repo.create_dependency(t2.id, t1.id)
        assert created is True

        deps = await repo.get_dependencies(t2.id)
        assert len(deps) == 1
        assert deps[0].prerequisite_task_id == t1.id

    async def test_cross_project_dependency_excluded_from_context(
        self, repo: MessageRepository, context_service: ContextService, db: Database
    ) -> None:
        """If a cross-project dependency exists in the DB (inserted via raw SQL),
        it must not appear in the project's context via
        get_all_dependencies_for_project."""
        project_a = await repo.create_project(server_id=3001, name="Project A")
        project_b = await repo.create_project(server_id=3001, name="Project B")

        await repo.add_project_channel(project_a.id, 2001, 3001)
        await repo.add_project_channel(project_b.id, 2002, 3001)

        t_a1 = await repo.create_task(project_a.id, "A Task 1")
        t_a2 = await repo.create_task(project_a.id, "A Task 2")
        t_b1 = await repo.create_task(project_b.id, "B Task 1")

        # Valid same-project dependency
        await repo.create_dependency(t_a2.id, t_a1.id)

        # Insert a cross-project dependency via raw SQL (bypassing validation)
        await db.execute(
            "INSERT INTO task_dependencies (task_id, prerequisite_task_id) VALUES (?, ?)",
            (t_a1.id, t_b1.id),
        )
        await db.commit()

        # Project A should only see its own dependency, not the cross-project one
        deps_a = await repo.get_all_dependencies_for_project(project_a.id)
        assert len(deps_a) == 1
        assert deps_a[0].task_id == t_a2.id
        assert deps_a[0].prerequisite_task_id == t_a1.id

        # The cross-project dependency (t_a1 -> t_b1) must not appear
        for dep in deps_a:
            assert dep.prerequisite_task_id != t_b1.id

        # Project B should have no dependencies at all
        deps_b = await repo.get_all_dependencies_for_project(project_b.id)
        assert len(deps_b) == 0

    async def test_cross_project_dependency_excluded_from_context_service(
        self, repo: MessageRepository, context_service: ContextService, db: Database
    ) -> None:
        """Full context retrieval must not include cross-project dependencies."""
        project_a = await repo.create_project(server_id=3001, name="Project A")
        project_b = await repo.create_project(server_id=3001, name="Project B")

        await repo.add_project_channel(project_a.id, 2001, 3001)
        await repo.add_project_channel(project_b.id, 2002, 3001)

        t_a1 = await repo.create_task(project_a.id, "A Task 1")
        t_a2 = await repo.create_task(project_a.id, "A Task 2")
        t_b1 = await repo.create_task(project_b.id, "B Task 1")

        # Valid same-project dependency
        await repo.create_dependency(t_a2.id, t_a1.id)

        # Cross-project dependency via raw SQL
        await db.execute(
            "INSERT INTO task_dependencies (task_id, prerequisite_task_id) VALUES (?, ?)",
            (t_a1.id, t_b1.id),
        )
        await db.commit()

        ctx_a = await context_service.get_context(channel_id=2001)
        assert len(ctx_a.task_dependencies) == 1
        assert ctx_a.task_dependencies[0].prerequisite_task_id == t_a1.id

        ctx_b = await context_service.get_context(channel_id=2002)
        assert len(ctx_b.task_dependencies) == 0


# ---------------------------------------------------------------------------
# E. Normal same-project context retrieval (regression guard)
# ---------------------------------------------------------------------------


class TestSameProjectContextRegression:
    async def test_full_context_retrieval_works(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """Verify that normal same-project context retrieval (messages,
        members, tasks, dependencies) works correctly after all fixes."""
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.add_project_channel(project.id, 2001, 3001)

        # Members
        await repo.upsert_member(project.id, 1001, "Alice")
        await repo.upsert_member(project.id, 1002, "Bob")

        # Messages
        for mid, ts in [(8001, "2025-01-15T10:00:00+00:00"),
                        (8002, "2025-01-15T12:00:00+00:00")]:
            await repo.insert_message(
                NormalizedMessage(
                    server_id=3001,
                    channel_id=2001,
                    message_id=mid,
                    author_id=1001,
                    author_display_name="Alice",
                    content=f"Message {mid}",
                    timestamp=datetime.fromisoformat(ts),
                )
            )

        # Tasks
        t1 = await repo.create_task(project.id, "Task 1")
        t2 = await repo.create_task(project.id, "Task 2")
        await repo.create_dependency(t2.id, t1.id)

        # Retrieve context
        ctx = await context_service.get_context(channel_id=2001)

        assert ctx.project is not None
        assert ctx.project.id == project.id
        assert len(ctx.recent_messages) == 2
        assert len(ctx.members) == 2
        assert len(ctx.tasks) == 2
        assert len(ctx.task_dependencies) == 1
        assert ctx.metadata.project_found is True
        assert ctx.metadata.has_messages is True
        assert ctx.metadata.has_members is True
        assert ctx.metadata.has_tasks is True
        assert ctx.metadata.has_dependencies is True

    async def test_context_via_message_still_works(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """Context retrieval via a NormalizedMessage should still work
        when server_id matches."""
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.add_project_channel(project.id, 2001, 3001)
        await repo.upsert_member(project.id, 1001, "Alice")

        msg = NormalizedMessage(
            server_id=3001,
            channel_id=2001,
            message_id=9001,
            author_id=1001,
            author_display_name="Alice",
            content="Hello",
            timestamp=datetime(2025, 1, 16, 10, 0, tzinfo=timezone.utc),
        )
        ctx = await context_service.get_context(message=msg)

        assert ctx.project is not None
        assert ctx.project.server_id == 3001
        assert ctx.current_message is msg
        assert len(ctx.members) == 1

    async def test_different_servers_both_work(
        self, repo: MessageRepository, context_service: ContextService
    ) -> None:
        """Two projects in different servers should both resolve correctly."""
        project_a = await repo.create_project(server_id=3001, name="Server A Project")
        project_b = await repo.create_project(server_id=9999, name="Server B Project")

        await repo.add_project_channel(project_a.id, 2001, 3001)
        await repo.add_project_channel(project_b.id, 7777, 9999)

        await repo.upsert_member(project_a.id, 1001, "Alice")
        await repo.upsert_member(project_b.id, 2001, "Bob")

        ctx_a = await context_service.get_context(channel_id=2001)
        ctx_b = await context_service.get_context(channel_id=7777)

        assert ctx_a.project.server_id == 3001
        assert ctx_b.project.server_id == 9999
        assert {m.discord_user_id for m in ctx_a.members} == {1001}
        assert {m.discord_user_id for m in ctx_b.members} == {2001}
