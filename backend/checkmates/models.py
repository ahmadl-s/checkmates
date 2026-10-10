"""Pydantic data models for normalized message data."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NormalizedMessage(BaseModel):
    """A normalized, Discord-agnostic representation of a collected message.

    This is the data object that flows from the collection layer to the
    downstream processing pipeline (context retrieval, AI extraction, etc.).
    The collector produces this; the processor consumes it.
    """

    model_config = ConfigDict(frozen=True)

    server_id: int = Field(description="Discord guild/server ID (0 for DMs)")
    channel_id: int = Field(description="Discord channel ID")
    message_id: int = Field(description="Discord message ID (unique per channel)")
    author_id: int = Field(description="Discord user ID of the message author")
    author_display_name: str | None = Field(
        default=None,
        description="Best available display name for the author",
    )
    content: str = Field(description="Message text content")
    timestamp: datetime = Field(description="When the message was originally sent (UTC)")
    edited_timestamp: datetime | None = Field(
        default=None,
        description="When the message was last edited, if applicable",
    )
    is_edit: bool = Field(
        default=False,
        description="True if this event was triggered by a message edit",
    )
