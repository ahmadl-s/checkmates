"""Database package: async SQLite persistence layer for checkmates.

Exports:
    Database            — connection management and schema init
    MessageRepository   — CRUD operations for all tables
    Project, Member, StoredMessage, Task, TaskDependency — DB entity models
"""

from checkmates.database.connection import Database
from checkmates.database.models import (
    Member,
    Project,
    StoredMessage,
    Task,
    TaskDependency,
)
from checkmates.database.repository import MessageRepository

__all__ = [
    "Database",
    "MessageRepository",
    "Project",
    "Member",
    "StoredMessage",
    "Task",
    "TaskDependency",
]
