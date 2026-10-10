"""Message normalization: converting Discord message objects to NormalizedMessage."""

from __future__ import annotations

from typing import Protocol

from checkmates.models import NormalizedMessage


class _AuthorLike(Protocol):
    """Structural type matching Discord author/member attributes we access."""

    @property
    def display_name(self) -> str: ...
    @property
    def global_name(self) -> str | None: ...
    @property
    def name(self) -> str: ...
    id: int


class _ChannelLike(Protocol):
    id: int


class _GuildLike(Protocol):
    id: int


class _MessageLike(Protocol):
    """Structural type matching the Discord message attributes we access."""

    id: int
    content: str
    author: _AuthorLike

    @property
    def channel(self) -> _ChannelLike: ...

    @property
    def guild(self) -> _GuildLike | None: ...

    @property
    def created_at(self): ...

    @property
    def edited_at(self): ...


class MessageNormalizer:
    """Converts Discord message objects into NormalizedMessage instances.

    This class is stateless; all methods are static for easy testing.
    The conversion is purely mechanical — no AI, no DB lookups.
    """

    @staticmethod
    def normalize(message: _MessageLike, *, is_edit: bool = False) -> NormalizedMessage:
        """Convert a Discord message into a NormalizedMessage.

        Args:
            message: A discord.Message (or mock with matching attributes).
            is_edit: True if this normalization is triggered by a message edit.

        Returns:
            A frozen NormalizedMessage ready for downstream processing.
        """
        guild = getattr(message, "guild", None)
        server_id = guild.id if guild is not None else 0

        return NormalizedMessage(
            server_id=server_id,
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=message.author.id,
            author_display_name=_resolve_display_name(message.author),
            content=message.content,
            timestamp=message.created_at,
            edited_timestamp=message.edited_at,
            is_edit=is_edit,
        )


def _resolve_display_name(author: _AuthorLike) -> str | None:
    """Extract the best available display name from a Discord author/member.

    Priority: display_name (server-specific) > global_name > name (username).
    """
    display_name = getattr(author, "display_name", None)
    if display_name:
        return display_name

    global_name = getattr(author, "global_name", None)
    if global_name:
        return global_name

    name = getattr(author, "name", None)
    return name if name else None
