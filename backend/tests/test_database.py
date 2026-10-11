"""Tests for database initialization, schema, CRUD, FK enforcement, and persistence."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from aiosqlite import IntegrityError

from checkmates.database import Database, MessageRepository
from checkmates.database.models import Member, Project, StoredMessage, Task
from checkmates.models import NormalizedMessage

from tests.conftest import MockMessage


# ---------------------------------------------------------------------------
# A. Database initialization and schema creation
# ---------------------------------------------------------------------------


class TestDatabaseInit:
    async def test_init_creates_tables(self, db: Database) -> None:
        """All 5 tables should be created."""
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {row["name"] for row in tables}
        assert "projects" in table_names
        assert "members" in table_names
        assert "messages" in table_names
        assert "tasks" in table_names
        assert "task_dependencies" in table_names

    async def test_init_creates_indexes(self, db: Database) -> None:
        """Indexes for common query patterns should be created."""
        indexes = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        )
        index_names = {row["name"] for row in indexes}
        assert "idx_messages_server_channel" in index_names
        assert "idx_messages_author" in index_names
        assert "idx_tasks_project" in index_names

    async def test_wal_mode_enabled(self, tmp_path) -> None:
        """WAL mode should be enabled for file-based databases.

        In-memory databases use 'memory' journal mode (SQLite default),
        so we test with a file-based DB to verify WAL is actually applied.
        """
        db_path = str(tmp_path / "test_wal.db")
        file_db = Database(db_path)
        await file_db.init()
        row = await file_db.fetchone("PRAGMA journal_mode")
        assert row[0] == "wal"
        await file_db.close()

    async def test_foreign_keys_enabled(self, db: Database) -> None:
        """Foreign-key constraints should be enforced."""
        row = await db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1

    async def test_init_is_idempotent(self, db: Database) -> None:
        """Calling init twice should not error or duplicate tables."""
        await db.init()  # second call
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        assert len(tables) == 1

    async def test_connection_raises_if_not_initialized(self) -> None:
        """Accessing connection before init() should raise."""
        db = Database(":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = db.connection


# ---------------------------------------------------------------------------
# B. Message insertion and retrieval
# ---------------------------------------------------------------------------


class TestMessagePersistence:
    async def test_insert_and_retrieve(
        self, repo: MessageRepository, normalized_message: NormalizedMessage
    ) -> None:
        inserted = await repo.insert_message(normalized_message)
        assert inserted is True

        retrieved = await repo.get_message_by_discord_id(normalized_message.message_id)
        assert retrieved is not None
        assert retrieved.discord_message_id == normalized_message.message_id
        assert retrieved.server_id == normalized_message.server_id
        assert retrieved.channel_id == normalized_message.channel_id
        assert retrieved.author_id == normalized_message.author_id
        assert retrieved.author_display_name == "Test User"
        assert retrieved.content == "I'll finish the backend API by Wednesday."
        assert retrieved.message_timestamp == normalized_message.timestamp
        assert retrieved.edited_timestamp is None
        assert retrieved.processed_at is not None

    async def test_retrieve_nonexistent_message(
        self, repo: MessageRepository
    ) -> None:
        retrieved = await repo.get_message_by_discord_id(999999999)
        assert retrieved is None

    async def test_get_messages_by_channel(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
        normalized_message_2: NormalizedMessage,
    ) -> None:
        await repo.insert_message(normalized_message)
        await repo.insert_message(normalized_message_2)

        messages = await repo.get_messages_by_channel(2001)
        assert len(messages) == 2
        # Ordered by timestamp DESC, so 5002 (12:00) comes before 5001 (10:00)
        assert messages[0].discord_message_id == 5002
        assert messages[1].discord_message_id == 5001

    async def test_get_messages_by_author(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
    ) -> None:
        await repo.insert_message(normalized_message)
        messages = await repo.get_messages_by_author(1001)
        assert len(messages) == 1
        assert messages[0].author_id == 1001


# ---------------------------------------------------------------------------
# C. Repeated insertion of the same message (idempotency)
# ---------------------------------------------------------------------------


class TestDuplicateInsertion:
    async def test_duplicate_insert_returns_false(
        self, repo: MessageRepository, normalized_message: NormalizedMessage
    ) -> None:
        first = await repo.insert_message(normalized_message)
        assert first is True

        second = await repo.insert_message(normalized_message)
        assert second is False  # already exists

    async def test_duplicate_insert_does_not_create_extra_rows(
        self, repo: MessageRepository, normalized_message: NormalizedMessage
    ) -> None:
        await repo.insert_message(normalized_message)
        await repo.insert_message(normalized_message)  # duplicate
        await repo.insert_message(normalized_message)  # triple duplicate

        messages = await repo.get_messages_by_channel(2001)
        assert len(messages) == 1

    async def test_is_message_persisted(
        self, repo: MessageRepository, normalized_message: NormalizedMessage
    ) -> None:
        assert await repo.is_message_persisted(5001) is False
        await repo.insert_message(normalized_message)
        assert await repo.is_message_persisted(5001) is True


# ---------------------------------------------------------------------------
# D. Message edits and updated content
# ---------------------------------------------------------------------------


class TestMessageEdits:
    async def test_update_edit_changes_content(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
        normalized_edit_message: NormalizedMessage,
    ) -> None:
        await repo.insert_message(normalized_message)
        updated = await repo.update_message_edit(normalized_edit_message)
        assert updated is True

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved.content == "Updated: I'll finish the backend API by Thursday."
        assert retrieved.edited_timestamp == datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)

    async def test_update_edit_nonexistent_returns_false(
        self, repo: MessageRepository, normalized_edit_message: NormalizedMessage
    ) -> None:
        updated = await repo.update_message_edit(normalized_edit_message)
        assert updated is False

    async def test_upsert_original_then_edit(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
        normalized_edit_message: NormalizedMessage,
    ) -> None:
        # First: original message
        inserted = await repo.upsert_message(normalized_message)
        assert inserted is True

        # Second: edit of the same message
        inserted = await repo.upsert_message(normalized_edit_message)
        assert inserted is False  # updated, not newly inserted

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved.content == "Updated: I'll finish the backend API by Thursday."
        assert retrieved.edited_timestamp is not None

    async def test_upsert_edit_without_original_inserts_new(
        self, repo: MessageRepository, normalized_edit_message: NormalizedMessage
    ) -> None:
        """Edge case: edit arrives before original — should insert as new."""
        inserted = await repo.upsert_message(normalized_edit_message)
        assert inserted is True

        retrieved = await repo.get_message_by_discord_id(5001)
        assert retrieved is not None
        assert retrieved.content == "Updated: I'll finish the backend API by Thursday."


# ---------------------------------------------------------------------------
# E. Project and member persistence
# ---------------------------------------------------------------------------


class TestProjectPersistence:
    async def test_create_and_retrieve_project(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Hackathon Bot")
        assert project.id is not None
        assert project.server_id == 3001
        assert project.name == "Hackathon Bot"

        retrieved = await repo.get_project(project.id)
        assert retrieved is not None
        assert retrieved.name == "Hackathon Bot"

    async def test_duplicate_project_returns_existing(self, repo: MessageRepository) -> None:
        p1 = await repo.create_project(server_id=3001, name="Project A")
        p2 = await repo.create_project(server_id=3001, name="Project A")
        assert p1.id == p2.id

    async def test_get_projects_by_server(self, repo: MessageRepository) -> None:
        await repo.create_project(server_id=3001, name="Project A")
        await repo.create_project(server_id=3001, name="Project B")
        await repo.create_project(server_id=3002, name="Project C")

        projects = await repo.get_projects_by_server(3001)
        assert len(projects) == 2


class TestMemberPersistence:
    async def test_upsert_member_creates_new(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        member = await repo.upsert_member(
            project_id=project.id,
            discord_user_id=1001,
            display_name="Test User",
        )
        assert member.discord_user_id == 1001
        assert member.display_name == "Test User"

    async def test_upsert_member_updates_display_name(
        self, repo: MessageRepository
    ) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.upsert_member(project.id, 1001, "Old Name")
        member = await repo.upsert_member(project.id, 1001, "New Name")
        assert member.display_name == "New Name"

    async def test_get_members_by_project(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.upsert_member(project.id, 1001, "User One")
        await repo.upsert_member(project.id, 1002, "User Two")

        members = await repo.get_members_by_project(project.id)
        assert len(members) == 2


# ---------------------------------------------------------------------------
# F. Task CRUD operations
# ---------------------------------------------------------------------------


class TestTaskCRUD:
    async def test_create_and_retrieve_task(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        task = await repo.create_task(
            project_id=project.id,
            title="Build API endpoint",
            description="Create the /api/tasks endpoint",
            assignee_id=1001,
            deadline=datetime(2025, 2, 1, 23, 59, tzinfo=timezone.utc),
        )
        assert task.id is not None
        assert task.title == "Build API endpoint"
        assert task.status == "pending"
        assert task.assignee_id == 1001

        retrieved = await repo.get_task(task.id)
        assert retrieved is not None
        assert retrieved.title == "Build API endpoint"

    async def test_update_task_status(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        task = await repo.create_task(project.id, "Do something")

        updated = await repo.update_task_status(task.id, "completed")
        assert updated is True

        retrieved = await repo.get_task(task.id)
        assert retrieved.status == "completed"

    async def test_update_task_fields(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        task = await repo.create_task(project.id, "Original Title")

        await repo.update_task(
            task.id,
            title="Updated Title",
            description="Updated description",
            status="in_progress",
        )

        retrieved = await repo.get_task(task.id)
        assert retrieved.title == "Updated Title"
        assert retrieved.description == "Updated description"
        assert retrieved.status == "in_progress"

    async def test_get_tasks_by_project(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.create_task(project.id, "Task 1")
        await repo.create_task(project.id, "Task 2")
        await repo.create_task(project.id, "Task 3")

        tasks = await repo.get_tasks_by_project(project.id)
        assert len(tasks) == 3

    async def test_get_tasks_by_assignee(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.create_task(project.id, "Task 1", assignee_id=1001)
        await repo.create_task(project.id, "Task 2", assignee_id=1002)
        await repo.create_task(project.id, "Task 3", assignee_id=1001)

        tasks = await repo.get_tasks_by_assignee(1001)
        assert len(tasks) == 2

    async def test_task_with_source_message(
        self,
        repo: MessageRepository,
        normalized_message: NormalizedMessage,
    ) -> None:
        await repo.insert_message(normalized_message)
        msg = await repo.get_message_by_discord_id(5001)

        project = await repo.create_project(server_id=3001, name="Test Project")
        task = await repo.create_task(
            project.id,
            "Finish API",
            source_message_id=msg.id,
        )
        assert task.source_message_id == msg.id


# ---------------------------------------------------------------------------
# G. Task dependency constraints
# ---------------------------------------------------------------------------


class TestTaskDependencies:
    async def test_create_dependency(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        t1 = await repo.create_task(project.id, "Task 1")
        t2 = await repo.create_task(project.id, "Task 2")

        created = await repo.create_dependency(t2.id, t1.id)
        assert created is True

        deps = await repo.get_dependencies(t2.id)
        assert len(deps) == 1
        assert deps[0].prerequisite_task_id == t1.id

    async def test_self_dependency_rejected(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        task = await repo.create_task(project.id, "Self-referencing task")

        with pytest.raises(Exception):  # CHECK constraint
            await repo.create_dependency(task.id, task.id)

    async def test_dependency_on_nonexistent_task_rejected(
        self, repo: MessageRepository
    ) -> None:
        """Dependency on a non-existent task must be rejected.

        The repository validates task existence before the SQL INSERT,
        raising ValueError with a descriptive message. Previously this
        was caught at the DB level as IntegrityError; the earlier
        validation provides a clearer error to callers.
        """
        project = await repo.create_project(server_id=3001, name="Test Project")
        t1 = await repo.create_task(project.id, "Task 1")

        with pytest.raises(ValueError, match="does not exist"):
            await repo.create_dependency(t1.id, 99999)

    async def test_duplicate_dependency_ignored(self, repo: MessageRepository) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        t1 = await repo.create_task(project.id, "Task 1")
        t2 = await repo.create_task(project.id, "Task 2")

        await repo.create_dependency(t2.id, t1.id)
        # Second attempt should be ignored (INSERT OR IGNORE)
        await repo.create_dependency(t2.id, t1.id)

        deps = await repo.get_dependencies(t2.id)
        assert len(deps) == 1


# ---------------------------------------------------------------------------
# H. Foreign-key and transaction behavior
# ---------------------------------------------------------------------------


class TestForeignKeyEnforcement:
    async def test_member_with_nonexistent_project_fails(
        self, db: Database
    ) -> None:
        with pytest.raises(IntegrityError):
            await db.execute(
                "INSERT INTO members (project_id, discord_user_id) VALUES (?, ?)",
                (99999, 1001),
            )
            await db.commit()

    async def test_task_with_nonexistent_project_fails(
        self, db: Database
    ) -> None:
        with pytest.raises(IntegrityError):
            await db.execute(
                "INSERT INTO tasks (project_id, title) VALUES (?, ?)",
                (99999, "Ghost task"),
            )
            await db.commit()

    async def test_cascade_delete_project_removes_members(
        self, repo: MessageRepository, db: Database
    ) -> None:
        project = await repo.create_project(server_id=3001, name="Test Project")
        await repo.upsert_member(project.id, 1001, "User One")
        await repo.upsert_member(project.id, 1002, "User Two")

        # Delete the project
        await db.execute("DELETE FROM projects WHERE id = ?", (project.id,))
        await db.commit()

        members = await repo.get_members_by_project(project.id)
        assert len(members) == 0  # cascaded


# ---------------------------------------------------------------------------
# I. Persistence across separate database connections (restarts)
# ---------------------------------------------------------------------------


class TestPersistenceAcrossConnections:
    async def test_data_survives_reconnect(
        self,
        tmp_path,
        normalized_message: NormalizedMessage,
    ) -> None:
        db_path = str(tmp_path / "test_persistence.db")

        # First connection: insert a message
        db1 = Database(db_path)
        await db1.init()
        repo1 = MessageRepository(db1)
        await repo1.insert_message(normalized_message)
        await db1.close()

        # Second connection: verify the message persists
        db2 = Database(db_path)
        await db2.init()
        repo2 = MessageRepository(db2)
        retrieved = await repo2.get_message_by_discord_id(5001)
        assert retrieved is not None
        assert retrieved.content == "I'll finish the backend API by Wednesday."
        await db2.close()

    async def test_dedup_survives_reconnect(
        self,
        tmp_path,
        normalized_message: NormalizedMessage,
    ) -> None:
        """The DB UNIQUE constraint provides persistent dedup across restarts."""
        db_path = str(tmp_path / "test_dedup.db")

        # First session: insert message
        db1 = Database(db_path)
        await db1.init()
        repo1 = MessageRepository(db1)
        await repo1.insert_message(normalized_message)
        await db1.close()

        # Second session: try to insert the same message
        db2 = Database(db_path)
        await db2.init()
        repo2 = MessageRepository(db2)
        inserted = await repo2.insert_message(normalized_message)
        assert inserted is False  # already persisted in first session
        await db2.close()
