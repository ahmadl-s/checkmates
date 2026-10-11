"""Pydantic models for database entities.

These models represent rows as returned from the database. They are separate
from the NormalizedMessage model (which is the collection-layer DTO) to keep
the database layer decoupled from the Discord collection layer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Project(BaseModel):
    """A project associated with a Discord server."""

    model_config = ConfigDict(frozen=True)

    id: int
    server_id: int
    name: str
    created_at: datetime
    updated_at: datetime


class Member(BaseModel):
    """A Discord user registered as a member of a project."""

    model_config = ConfigDict(frozen=True)

    id: int
    project_id: int
    discord_user_id: int
    display_name: str | None
    created_at: datetime
    updated_at: datetime


class StoredMessage(BaseModel):
    """A message as stored in the database (after collection + persistence)."""

    model_config = ConfigDict(frozen=True)

    id: int
    discord_message_id: int
    server_id: int
    channel_id: int
    author_id: int
    author_display_name: str | None
    content: str
    message_timestamp: datetime
    edited_timestamp: datetime | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class Task(BaseModel):
    """A task extracted from conversations or manually created."""

    model_config = ConfigDict(frozen=True)

    id: int
    project_id: int
    title: str
    description: str | None = None
    assignee_id: int | None = None
    # The DB column is TEXT. Manually created tasks store ISO datetime
    # strings; AI-extracted tasks store raw deadline text (e.g., "by Friday").
    # Using str | None so both round-trip without Pydantic parsing errors.
    deadline: str | None = None
    status: str = "pending"
    source_message_id: int | None = None
    created_at: datetime
    updated_at: datetime


class TaskDependency(BaseModel):
    """A dependency: task_id cannot be completed until prerequisite_task_id is done."""

    model_config = ConfigDict(frozen=True)

    task_id: int
    prerequisite_task_id: int
    created_at: datetime
