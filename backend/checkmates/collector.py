"""Discord message collector: listens to events, filters, normalizes, deduplicates.

This is the entry point of the collection pipeline. It is a discord.Client
subclass that wires together MessageFilter, MessageNormalizer, and
DeduplicationCache. Normalized messages are forwarded to a downstream
MessageProcessor.

Design note: The collector deliberately does NOT import the AI pipeline,
database layer, or any future feature. It only knows about the processor
interface, keeping the collection layer cleanly separated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from checkmates.config import Settings
from checkmates.dedup import DeduplicationCache
from checkmates.filtering import MessageFilter
from checkmates.normalizer import MessageNormalizer
from checkmates.processors.base import MessageProcessor

if TYPE_CHECKING:
    from checkmates.database.repository import MessageRepository

logger = logging.getLogger(__name__)

# Discord's maximum message length.
_DISCORD_MSG_LIMIT = 2000

# Command prefix for bot commands.
_COMMAND_PREFIX = "!"


def _split_message(text: str, max_len: int = _DISCORD_MSG_LIMIT) -> list[str]:
    """Split text into chunks that fit within Discord's message length limit.

    Splits on newline boundaries when possible. If a single line exceeds
    max_len, splits at word boundaries.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > max_len:
            chunks.append(current)
            current = line
        elif len(line) > max_len:
            # Line itself exceeds limit — split at word boundaries
            if current:
                chunks.append(current)
                current = ""
            for word in line.split(" "):
                if current and len(current) + 1 + len(word) > max_len:
                    chunks.append(current)
                    current = word
                else:
                    current = f"{current} {word}" if current else word
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)

    return chunks


class MessageCollector(discord.Client):
    """Discord client that collects and normalizes messages from project channels.

    Lifecycle:
        on_message       -> [command check] -> filter -> dedup -> normalize -> processor.process()
        on_message_edit  -> filter -> dedup -> normalize(edit) -> processor.process()

    When a repository is provided, the collector can handle simple commands
    like ``!tasks`` before the collection pipeline. Command messages are
    never forwarded to the processor.

    The processor is the seam where AI extraction, context management, and
    database operations are wired in.
    """

    def __init__(
        self,
        settings: Settings,
        processor: MessageProcessor,
        *,
        repository: MessageRepository | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.members = True  # needed for author.display_name
        super().__init__(intents=intents)

        self._settings = settings
        self._processor = processor
        self._repository = repository
        self._filter = MessageFilter(settings.channel_ids)
        self._normalizer = MessageNormalizer()
        self._dedup = DeduplicationCache()

    async def on_message(self, message: discord.Message) -> None:
        """Handle new messages from Discord.

        Intercepts ``!tasks`` commands before the collection pipeline.
        All other messages go through filter -> dedup -> normalize -> process.
        """
        # Command interception (before filter — commands work in any channel
        # with a project mapping, not just monitored channels)
        if self._repository is not None and not getattr(
            message.author, "bot", False
        ):
            content = message.content.strip().lower()
            if content == f"{_COMMAND_PREFIX}tasks":
                await self._handle_tasks_command(message)
                return

        await self._handle_message(message, is_edit=False)

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Handle message edits — forward the edited version."""
        await self._handle_message(after, is_edit=True)

    async def _handle_message(self, message: discord.Message, *, is_edit: bool) -> None:
        """Core processing: filter, dedup-check, normalize, forward, dedup-mark.

        The in-memory dedup cache is checked BEFORE processing (fast path to
        skip already-handled messages) but only marked AFTER successful
        processing. If processor.process() raises, the message is NOT marked
        and will be retried on the next event. The database UNIQUE constraint
        on discord_message_id provides authoritative persistent idempotency.

        Any exception is caught and logged so one bad message doesn't crash
        the collection loop. Message content is deliberately NOT logged.
        """
        try:
            if not self._filter.should_process(message):
                return

            # Check in-memory cache (read-only — does NOT mark yet)
            if is_edit:
                if self._dedup.is_seen_edit(message.id, message.edited_at):
                    return
            else:
                if self._dedup.is_seen_original(message.id):
                    return

            normalized = self._normalizer.normalize(message, is_edit=is_edit)

            logger.info(
                "Forwarding message id=%s channel=%s author=%s edit=%s",
                normalized.message_id,
                normalized.channel_id,
                normalized.author_id,
                normalized.is_edit,
            )

            # Process the message — if this raises, we don't mark the dedup cache
            await self._processor.process(normalized)

            # Mark as seen ONLY after successful processing
            if is_edit:
                self._dedup.mark_edit(message.id, message.edited_at)
            else:
                self._dedup.mark_original(message.id)

        except Exception:
            logger.exception(
                "Error processing message id=%s channel=%s",
                getattr(message, "id", "unknown"),
                getattr(getattr(message, "channel", None), "id", "unknown"),
            )

    async def _handle_tasks_command(self, message: discord.Message) -> None:
        """Handle the ``!tasks`` command: list saved tasks for the channel's project.

        Resolves the project from the channel via the repository, retrieves
        all tasks for that project, formats them, and sends the result.
        Project isolation is enforced by ``get_project_by_channel`` — only
        tasks belonging to the project mapped to this channel are returned.

        Never calls Gemini or any AI service — reads from SQLite only.
        """
        assert self._repository is not None  # checked by caller

        try:
            project = await self._repository.get_project_by_channel(
                message.channel.id
            )
            if project is None:
                await message.channel.send(
                    "No project is mapped to this channel. "
                    "Ask an admin to associate this channel with a project."
                )
                return

            tasks = await self._repository.get_tasks_by_project(project.id)

            if not tasks:
                await message.channel.send(
                    f"No tasks have been recorded for **{project.name}** yet. "
                    "Send task-related messages in this channel and I'll "
                    "extract them automatically!"
                )
                return

            # Resolve assignee display names from project members
            members = await self._repository.get_members_by_project(project.id)
            member_names: dict[int, str] = {
                m.discord_user_id: m.display_name or f"User {m.discord_user_id}"
                for m in members
            }

            # Format task list
            lines = [f"**Tasks for {project.name}** ({len(tasks)})\n"]
            for i, task in enumerate(tasks, 1):
                if task.assignee_id is not None:
                    assignee = member_names.get(
                        task.assignee_id, f"User {task.assignee_id}"
                    )
                else:
                    assignee = "Unassigned"
                deadline = task.deadline or "Not set"
                lines.append(
                    f"{i}. **{task.title}** `[{task.status}]` "
                    f"— Assignee: {assignee} | Deadline: {deadline}"
                )

            # Split and send, respecting Discord's message-length limit
            full_text = "\n".join(lines)
            chunks = _split_message(full_text)
            for chunk in chunks:
                await message.channel.send(chunk)

            logger.info(
                "Tasks command: returned %d task(s) for project '%s' in channel %s",
                len(tasks),
                project.name,
                message.channel.id,
            )

        except Exception:
            logger.exception(
                "Error handling !tasks command in channel id=%s",
                getattr(message.channel, "id", "unknown"),
            )
            try:
                await message.channel.send(
                    "Sorry, I couldn't retrieve tasks right now. "
                    "Please try again later."
                )
            except Exception:
                pass
