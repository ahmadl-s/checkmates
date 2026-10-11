"""Pydantic models for project context.

These models package the information retrieved from the database into a
single, predictable, serializable object that downstream consumers (AI
extraction, conflict detection, analytics) can use without making their
own database queries.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from checkmates.database.models import (
    Member,
    Project,
    StoredMessage,
    Task,
    TaskDependency,
)
from checkmates.models import NormalizedMessage


class ContextMetadata(BaseModel):
    """Flags indicating the completeness of the retrieved context.

    Downstream consumers can use these to decide whether to proceed
    or request clarification without inspecting every list for emptiness.
    """

    project_found: bool = Field(
        default=False, description="True if a project was resolved for the channel"
    )
    has_messages: bool = Field(
        default=False, description="True if at least one project message exists"
    )
    has_tasks: bool = Field(
        default=False, description="True if at least one task exists for the project"
    )
    has_members: bool = Field(
        default=False, description="True if at least one member is registered"
    )
    has_dependencies: bool = Field(
        default=False, description="True if any task dependencies exist"
    )
    max_messages: int = Field(
        description="The message limit used for this context retrieval"
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this context was assembled (UTC)",
    )


class ProjectContext(BaseModel):
    """Structured project context for AI extraction and agentic workflows.

    This is the single object returned by ContextService.get_context().
    It contains everything the AI pipeline needs to understand the
    current message in the context of the project's history.
    """

    project: Project | None = Field(
        default=None,
        description="The resolved project, or None if the channel is not registered",
    )
    current_message: NormalizedMessage | None = Field(
        default=None,
        description="The triggering message, if context was requested for one",
    )
    recent_messages: list[StoredMessage] = Field(
        default_factory=list,
        description="Recent project messages in chronological order (oldest first)",
    )
    members: list[Member] = Field(
        default_factory=list,
        description="All registered members of the project",
    )
    tasks: list[Task] = Field(
        default_factory=list,
        description="All tasks belonging to the project",
    )
    task_dependencies: list[TaskDependency] = Field(
        default_factory=list,
        description="All task dependencies within the project",
    )
    metadata: ContextMetadata = Field(
        description="Completeness flags and retrieval parameters",
    )
