"""Project context retrieval service.

This is an independent service that gathers project-scoped information from
the database and returns it as a single ProjectContext object. It is NOT
wired into the Discord collector or the PersistenceProcessor — it is
called by downstream consumers (AI extraction, conflict detection, etc.)
when they need to understand a message in its project context.

Design decisions:
    - Message-to-project resolution: a message's channel_id is looked up
      in the project_channels table. If no project is registered for that
      channel, project is None and all lists are empty.
    - Project/server isolation: all queries are scoped by project_id.
      A message can only belong to one channel, which belongs to one
      project, so there is no way to leak cross-project data.
    - Message ordering: the most recent N messages are retrieved
      efficiently (DESC + LIMIT), then reversed to chronological order.
    - No invented data: if the database is empty, the context is empty.
      The service never creates projects, members, or tasks.
"""

from __future__ import annotations

import logging

from checkmates.context.models import ContextMetadata, ProjectContext
from checkmates.database.repository import MessageRepository
from checkmates.models import NormalizedMessage

logger = logging.getLogger(__name__)

# Default maximum number of recent messages to include in context.
_DEFAULT_MAX_MESSAGES = 50


class ContextService:
    """Retrieves structured project context from the database.

    Usage:
        service = ContextService(repo, max_messages=50)
        context = await service.get_context(message=normalized_msg)
        # context.project, context.recent_messages, context.tasks, ...

    Or without a specific message (e.g., for a dashboard or scheduled check):
        context = await service.get_context(channel_id=2001)

    Lifecycle: stateless — can be called concurrently from different
    async tasks without internal locking.
    """

    def __init__(
        self,
        repository: MessageRepository,
        *,
        max_messages: int = _DEFAULT_MAX_MESSAGES,
    ) -> None:
        self._repo = repository
        self._max_messages = max(1, max_messages)  # at least 1

    @property
    def max_messages(self) -> int:
        """The configured maximum number of recent messages to retrieve."""
        return self._max_messages

    async def get_context(
        self,
        *,
        message: NormalizedMessage | None = None,
        channel_id: int | None = None,
    ) -> ProjectContext:
        """Retrieve full project context.

        Args:
            message: A NormalizedMessage to resolve the project from.
                If provided, it is included as current_message in the
                returned context.
            channel_id: A Discord channel ID to resolve the project from.
                Used when there is no specific message (e.g., a dashboard
                query). Ignored if ``message`` is also provided.

        Returns:
            A ProjectContext with all available project data. If no
            project is found for the channel, all lists are empty and
            project is None. No data is invented. If a message is
            provided and its server_id does not match the resolved
            project's server_id, the context is returned empty
            (project=None) to prevent cross-server data leakage.

        Raises:
            ValueError: If neither ``message`` nor ``channel_id`` is provided.
        """
        if message is not None:
            resolved_channel_id = message.channel_id
            current_message = message
        elif channel_id is not None:
            resolved_channel_id = channel_id
            current_message = None
        else:
            raise ValueError(
                "get_context requires either 'message' or 'channel_id'"
            )

        # Resolve project from the channel
        project = await self._repo.get_project_by_channel(resolved_channel_id)

        # If a message is provided, verify its server_id matches the project's
        # server_id. A mismatch means the message is from a different server
        # than the one the channel is registered to — treat as no project found
        # to prevent cross-server context leakage.
        if (
            project is not None
            and message is not None
            and message.server_id != project.server_id
        ):
            logger.warning(
                "Server ID mismatch: message server_id=%s does not match "
                "project server_id=%s for channel_id=%s — returning empty context",
                message.server_id,
                project.server_id,
                resolved_channel_id,
            )
            project = None

        # Build metadata
        metadata = ContextMetadata(
            project_found=project is not None,
            max_messages=self._max_messages,
        )

        if project is None:
            logger.debug(
                "No project found for channel_id=%s — returning empty context",
                resolved_channel_id,
            )
            return ProjectContext(
                project=None,
                current_message=current_message,
                recent_messages=[],
                members=[],
                tasks=[],
                task_dependencies=[],
                metadata=metadata,
            )

        # Retrieve all project-scoped data in parallel-safe sequence
        recent_messages = await self._repo.get_messages_by_project(
            project.id, limit=self._max_messages
        )
        members = await self._repo.get_members_by_project(project.id)
        tasks = await self._repo.get_tasks_by_project(project.id)
        task_deps = await self._repo.get_all_dependencies_for_project(project.id)

        # Update metadata flags
        metadata.has_messages = len(recent_messages) > 0
        metadata.has_tasks = len(tasks) > 0
        metadata.has_members = len(members) > 0
        metadata.has_dependencies = len(task_deps) > 0

        logger.debug(
            "Context retrieved for project_id=%s name=%s: "
            "%d messages, %d members, %d tasks, %d dependencies",
            project.id,
            project.name,
            len(recent_messages),
            len(members),
            len(tasks),
            len(task_deps),
        )

        return ProjectContext(
            project=project,
            current_message=current_message,
            recent_messages=recent_messages,
            members=members,
            tasks=tasks,
            task_dependencies=task_deps,
            metadata=metadata,
        )
