"""Repository: data access layer for all database operations.

All SQL is centralized here. Discord event handlers and processors never
write raw SQL — they go through this class.

Idempotency strategy:
    - Original messages: INSERT OR IGNORE with UNIQUE(discord_message_id).
      Duplicate events are silently ignored (no duplicate rows).
    - Edited messages: UPDATE the existing row. If the row doesn't exist
      yet (edge case), insert it as a new message.
    - Tasks, projects, members: use appropriate UNIQUE constraints and
      INSERT OR IGNORE / INSERT OR REPLACE patterns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from checkmates.database.connection import Database
from checkmates.database.models import (
    Member,
    Project,
    StoredMessage,
    Task,
    TaskDependency,
)
from checkmates.models import NormalizedMessage

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class MessageRepository:
    """Data access layer for messages, projects, members, tasks, and dependencies.

    Constructed with a Database instance (must be initialized before use).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def insert_message(self, msg: NormalizedMessage) -> bool:
        """Insert a new message (idempotent via INSERT OR IGNORE).

        Returns True if a new row was inserted, False if it already existed.
        """
        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO messages
                (discord_message_id, server_id, channel_id, author_id,
                 author_display_name, content, message_timestamp,
                 edited_timestamp, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.message_id,
                msg.server_id,
                msg.channel_id,
                msg.author_id,
                msg.author_display_name,
                msg.content,
                msg.timestamp.isoformat(),
                msg.edited_timestamp.isoformat() if msg.edited_timestamp else None,
                _utc_now_iso(),
            ),
        )
        await self._db.commit()
        inserted = cursor.rowcount > 0
        if inserted:
            logger.info("Inserted message discord_id=%s", msg.message_id)
        else:
            logger.debug("Message discord_id=%s already exists (ignored)", msg.message_id)
        return inserted

    async def update_message_edit(self, msg: NormalizedMessage) -> bool:
        """Update an existing message with edited content.

        Returns True if a row was updated, False if the message didn't exist.
        """
        cursor = await self._db.execute(
            """
            UPDATE messages SET
                content = ?,
                edited_timestamp = ?,
                author_display_name = ?,
                updated_at = datetime('now'),
                processed_at = ?
            WHERE discord_message_id = ?
            """,
            (
                msg.content,
                msg.edited_timestamp.isoformat() if msg.edited_timestamp else None,
                msg.author_display_name,
                _utc_now_iso(),
                msg.message_id,
            ),
        )
        await self._db.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Updated message edit discord_id=%s", msg.message_id)
        return updated

    async def upsert_message(self, msg: NormalizedMessage) -> bool:
        """Insert if new, update if edit. Returns True if a new row was created.

        This is the primary persistence method called by PersistenceProcessor.
        For original messages: insert (idempotent).
        For edited messages: update existing, or insert if missing.
        """
        if msg.is_edit:
            updated = await self.update_message_edit(msg)
            if not updated:
                # Edge case: edit arrived before original — insert as new
                await self.insert_message(msg)
                return True
            return False
        else:
            return await self.insert_message(msg)

    async def get_message_by_discord_id(
        self, discord_message_id: int
    ) -> StoredMessage | None:
        """Retrieve a message by its Discord message ID."""
        row = await self._db.fetchone(
            "SELECT * FROM messages WHERE discord_message_id = ?",
            (discord_message_id,),
        )
        if row is None:
            return None
        return StoredMessage(**dict(row))

    async def get_messages_by_channel(
        self, channel_id: int, limit: int = 100
    ) -> list[StoredMessage]:
        """Retrieve recent messages for a channel, ordered by timestamp."""
        rows = await self._db.fetchall(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY message_timestamp DESC LIMIT ?",
            (channel_id, limit),
        )
        return [StoredMessage(**dict(r)) for r in rows]

    async def get_messages_by_author(
        self, author_id: int, limit: int = 100
    ) -> list[StoredMessage]:
        """Retrieve recent messages by an author, ordered by timestamp."""
        rows = await self._db.fetchall(
            "SELECT * FROM messages WHERE author_id = ? ORDER BY message_timestamp DESC LIMIT ?",
            (author_id, limit),
        )
        return [StoredMessage(**dict(r)) for r in rows]

    async def is_message_persisted(self, discord_message_id: int) -> bool:
        """Check if a message has already been persisted (persistent dedup check)."""
        row = await self._db.fetchone(
            "SELECT 1 FROM messages WHERE discord_message_id = ?",
            (discord_message_id,),
        )
        return row is not None

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def create_project(self, server_id: int, name: str) -> Project:
        """Create a project. If it already exists (server_id + name), return the existing one."""
        existing = await self._db.fetchone(
            "SELECT * FROM projects WHERE server_id = ? AND name = ?",
            (server_id, name),
        )
        if existing is not None:
            return Project(**dict(existing))

        cursor = await self._db.execute(
            "INSERT INTO projects (server_id, name) VALUES (?, ?)",
            (server_id, name),
        )
        await self._db.commit()
        row = await self._db.fetchone(
            "SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)
        )
        return Project(**dict(row))

    async def get_project(self, project_id: int) -> Project | None:
        """Retrieve a project by ID."""
        row = await self._db.fetchone(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        return Project(**dict(row)) if row else None

    async def get_projects_by_server(self, server_id: int) -> list[Project]:
        """Retrieve all projects for a Discord server."""
        rows = await self._db.fetchall(
            "SELECT * FROM projects WHERE server_id = ? ORDER BY created_at",
            (server_id,),
        )
        return [Project(**dict(r)) for r in rows]

    async def add_project_channel(
        self, project_id: int, channel_id: int, server_id: int
    ) -> bool:
        """Associate a Discord channel with a project.

        Each channel can belong to at most one project (UNIQUE constraint).
        The channel's server_id must match the project's server_id.

        Returns True if a new association was created, False if it already existed.

        Raises:
            ValueError: If the project doesn't exist or server_id doesn't match
                the project's server_id.
        """
        project = await self.get_project(project_id)
        if project is None:
            raise ValueError(f"Project {project_id} does not exist")
        if project.server_id != server_id:
            raise ValueError(
                f"Server ID mismatch: channel server_id={server_id} "
                f"does not match project server_id={project.server_id}"
            )

        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO project_channels (project_id, channel_id, server_id)
            VALUES (?, ?, ?)
            """,
            (project_id, channel_id, server_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_project_by_channel(self, channel_id: int) -> Project | None:
        """Resolve a project from a Discord channel ID.

        Returns None if no project is registered for this channel.
        """
        row = await self._db.fetchone(
            """
            SELECT p.* FROM projects p
            JOIN project_channels pc ON pc.project_id = p.id
            WHERE pc.channel_id = ?
            """,
            (channel_id,),
        )
        return Project(**dict(row)) if row else None

    async def get_channels_by_project(self, project_id: int) -> list[int]:
        """Return all Discord channel IDs registered for a project."""
        rows = await self._db.fetchall(
            "SELECT channel_id FROM project_channels WHERE project_id = ?",
            (project_id,),
        )
        return [row["channel_id"] for row in rows]

    async def get_messages_by_project(
        self, project_id: int, limit: int = 50
    ) -> list[StoredMessage]:
        """Retrieve recent messages across all channels of a project.

        Only messages whose server_id matches the project's server_id are
        returned, ensuring cross-server isolation even if the database
        contains inconsistent records.

        Returns the most recent N messages (DESC order from DB) reversed to
        chronological (ascending) order. No duplicates — each message belongs
        to exactly one channel, which belongs to exactly one project.
        """
        rows = await self._db.fetchall(
            """
            SELECT m.* FROM messages m
            JOIN project_channels pc ON pc.channel_id = m.channel_id
            JOIN projects p ON p.id = pc.project_id
            WHERE pc.project_id = ? AND m.server_id = p.server_id
            ORDER BY m.message_timestamp DESC
            LIMIT ?
            """,
            (project_id, limit),
        )
        messages = [StoredMessage(**dict(r)) for r in rows]
        messages.reverse()  # chronological (oldest first)
        return messages

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    async def upsert_member(
        self, project_id: int, discord_user_id: int, display_name: str | None = None
    ) -> Member:
        """Register or update a member in a project.

        If the member already exists, update their display name.
        """
        existing = await self._db.fetchone(
            "SELECT * FROM members WHERE project_id = ? AND discord_user_id = ?",
            (project_id, discord_user_id),
        )
        if existing is not None:
            cursor = await self._db.execute(
                """UPDATE members SET display_name = ?, updated_at = datetime('now')
                   WHERE project_id = ? AND discord_user_id = ?""",
                (display_name, project_id, discord_user_id),
            )
            await self._db.commit()
            row = await self._db.fetchone(
                "SELECT * FROM members WHERE project_id = ? AND discord_user_id = ?",
                (project_id, discord_user_id),
            )
            return Member(**dict(row))

        cursor = await self._db.execute(
            "INSERT INTO members (project_id, discord_user_id, display_name) VALUES (?, ?, ?)",
            (project_id, discord_user_id, display_name),
        )
        await self._db.commit()
        row = await self._db.fetchone(
            "SELECT * FROM members WHERE id = ?", (cursor.lastrowid,)
        )
        return Member(**dict(row))

    async def get_members_by_project(self, project_id: int) -> list[Member]:
        """Retrieve all members of a project."""
        rows = await self._db.fetchall(
            "SELECT * FROM members WHERE project_id = ? ORDER BY discord_user_id",
            (project_id,),
        )
        return [Member(**dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_task(
        self,
        project_id: int,
        title: str,
        *,
        description: str | None = None,
        assignee_id: int | None = None,
        deadline: datetime | None = None,
        status: str = "pending",
        source_message_id: int | None = None,
    ) -> Task:
        """Create a new task and return it."""
        cursor = await self._db.execute(
            """
            INSERT INTO tasks
                (project_id, title, description, assignee_id, deadline,
                 status, source_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                title,
                description,
                assignee_id,
                deadline.isoformat() if deadline else None,
                status,
                source_message_id,
            ),
        )
        await self._db.commit()
        row = await self._db.fetchone(
            "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
        )
        return Task(**dict(row))

    async def save_extracted_task(
        self,
        project_id: int,
        title: str,
        *,
        description: str | None = None,
        assignee_id: int | None = None,
        deadline_text: str | None = None,
        source_message_id: int | None = None,
    ) -> Task | None:
        """Save a task extracted by the AI. Idempotent via unique index.

        Uses INSERT OR IGNORE so that re-processing the same message with
        the same task title does not create a duplicate. The unique
        partial index ``idx_tasks_source_title`` on
        ``(source_message_id, title) WHERE source_message_id IS NOT NULL``
        enforces this at the database level.

        Args:
            project_id: The project this task belongs to.
            title: Task title from AI extraction.
            description: Optional task description from AI.
            assignee_id: Resolved Discord user ID (may be None if no
                assignee was identified or the name was not matched).
            deadline_text: Deadline as expressed in the message text
                (e.g., "by Friday"). Stored as-is in the TEXT column.
                May be None if no deadline was identified.
            source_message_id: Internal messages.id (NOT the Discord
                message ID). Must be resolved via get_message_by_discord_id()
                before calling this method.

        Returns:
            The created Task if a new row was inserted, None if it was
            a duplicate (ignored by INSERT OR IGNORE).
        """
        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO tasks
                (project_id, title, description, assignee_id, deadline,
                 status, source_message_id)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                project_id,
                title,
                description,
                assignee_id,
                deadline_text,
                source_message_id,
            ),
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            row = await self._db.fetchone(
                "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
            )
            return Task(**dict(row))
        return None

    async def get_task(self, task_id: int) -> Task | None:
        """Retrieve a task by ID."""
        row = await self._db.fetchone(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        return Task(**dict(row)) if row else None

    async def get_tasks_by_project(self, project_id: int) -> list[Task]:
        """Retrieve all tasks for a project."""
        rows = await self._db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        return [Task(**dict(r)) for r in rows]

    async def get_tasks_by_assignee(self, assignee_id: int) -> list[Task]:
        """Retrieve all tasks assigned to a Discord user."""
        rows = await self._db.fetchall(
            "SELECT * FROM tasks WHERE assignee_id = ? ORDER BY created_at",
            (assignee_id,),
        )
        return [Task(**dict(r)) for r in rows]

    async def update_task_status(self, task_id: int, status: str) -> bool:
        """Update a task's status. Returns True if updated."""
        cursor = await self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        assignee_id: int | None = None,
        deadline: datetime | None = None,
        status: str | None = None,
    ) -> bool:
        """Update task fields. Only provided (non-None) fields are changed.

        Note: assignee_id and deadline use a sentinel to distinguish
        'not provided' from 'set to None'.
        """
        updates: list[str] = []
        params: list = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if assignee_id is not None:
            updates.append("assignee_id = ?")
            params.append(assignee_id)
        if deadline is not None:
            updates.append("deadline = ?")
            params.append(deadline.isoformat())
        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if not updates:
            return False

        updates.append("updated_at = datetime('now')")
        params.append(task_id)

        cursor = await self._db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Task Dependencies
    # ------------------------------------------------------------------

    async def create_dependency(
        self, task_id: int, prerequisite_task_id: int
    ) -> bool:
        """Create a task dependency. Returns True if created, False if it already existed.

        Both tasks must belong to the same project. Self-dependencies and
        references to non-existent tasks are rejected by database constraints.

        Uses ON CONFLICT to ignore duplicate PK entries only. CHECK constraint
        violations (self-dependency) and FK violations (non-existent task)
        will still raise IntegrityError.

        Raises:
            ValueError: If either task doesn't exist or they belong to
                different projects.
        """
        task = await self.get_task(task_id)
        prerequisite = await self.get_task(prerequisite_task_id)
        if task is None or prerequisite is None:
            raise ValueError(
                f"Task {task_id} or {prerequisite_task_id} does not exist"
            )
        if task.project_id != prerequisite.project_id:
            raise ValueError(
                f"Cross-project dependency rejected: task {task_id} belongs to "
                f"project {task.project_id}, but prerequisite "
                f"{prerequisite_task_id} belongs to project "
                f"{prerequisite.project_id}"
            )

        try:
            await self._db.execute(
                """INSERT INTO task_dependencies (task_id, prerequisite_task_id)
                   VALUES (?, ?)
                   ON CONFLICT(task_id, prerequisite_task_id) DO NOTHING""",
                (task_id, prerequisite_task_id),
            )
            await self._db.commit()
            return True
        except Exception as exc:
            logger.warning(
                "Failed to create dependency %s -> %s: %s",
                task_id,
                prerequisite_task_id,
                exc,
            )
            raise

    async def get_dependencies(self, task_id: int) -> list[TaskDependency]:
        """Retrieve all prerequisites for a task."""
        rows = await self._db.fetchall(
            "SELECT * FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        return [TaskDependency(**dict(r)) for r in rows]

    async def get_all_dependencies_for_project(
        self, project_id: int
    ) -> list[TaskDependency]:
        """Retrieve all task dependencies for every task in a project.

        Only dependencies where both the task and its prerequisite belong
        to the same project are returned, ensuring cross-project dependencies
        (if any exist due to direct DB manipulation) are excluded.
        """
        rows = await self._db.fetchall(
            """
            SELECT td.* FROM task_dependencies td
            JOIN tasks t ON td.task_id = t.id
            JOIN tasks pt ON td.prerequisite_task_id = pt.id
            WHERE t.project_id = ? AND pt.project_id = t.project_id
            """,
            (project_id,),
        )
        return [TaskDependency(**dict(r)) for r in rows]
