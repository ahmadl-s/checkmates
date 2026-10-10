"""Message filtering: deciding which Discord messages to collect."""

from __future__ import annotations

from typing import Protocol


class _MessageLike(Protocol):
    """Structural type matching the Discord message attributes we access."""

    class _AuthorLike(Protocol):
        bot: bool

    @property
    def author(self) -> _AuthorLike: ...

    class _ChannelLike(Protocol):
        id: int

    @property
    def channel(self) -> _ChannelLike: ...

    content: str


class MessageFilter:
    """Filters Discord messages based on configured rules.

    Rules:
        1. Reject messages from bots (including ourselves).
        2. Reject messages outside configured project channels.
        3. Reject empty messages (embed-only, sticker-only, etc.).
    """

    def __init__(self, channel_ids: set[int]) -> None:
        self._channel_ids = frozenset(channel_ids)

    def should_process(self, message: _MessageLike) -> bool:
        """Return True if the message should be collected and processed."""
        # 1. Ignore bot-generated messages
        if getattr(message.author, "bot", False):
            return False

        # 2. Only process messages from configured project channels
        channel_id = getattr(message.channel, "id", None)
        if channel_id not in self._channel_ids:
            return False

        # 3. Ignore empty messages (embeds/stickers without text)
        content = getattr(message, "content", "")
        if not content or not content.strip():
            return False

        return True
