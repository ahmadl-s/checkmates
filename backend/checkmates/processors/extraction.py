"""ExtractionProcessor: persists messages, then enqueues AI extraction.

Wraps a PersistenceProcessor (which persists to DB first), then enqueues
the message for background Gemini extraction via a bounded asyncio.Queue
with a single background worker task.

Design:
    process()  -> wrapped_processor.process() (persist)
               -> queue.put_nowait() (enqueue for extraction)

    _worker()  -> queue.get() -> extraction_service.extract()
                -> if actionable tasks and repository provided:
                   persist each task to the tasks table
                -> if completions and repository provided:
                   match and mark existing tasks as completed

    shutdown() -> set _stopped (stop accepting new extraction work)
               -> wait for queue to drain (configurable timeout)
               -> cancel worker if still running

The message is ALWAYS persisted before extraction is enqueued. If the
queue is full, the message remains persisted and a warning is logged
with message_id and channel_id (no message content in logs).

When a repository is provided:
- Actionable extracted tasks are persisted to the tasks table via
  save_extracted_task() with INSERT OR IGNORE (dedup via unique index).
- Task completions are matched against existing project tasks by title
  (case-insensitive). If exactly one pending task matches, it is marked
  completed. Ambiguous or unmatched completions are logged without
  changes. Already-completed tasks are not touched.

ExtractionService.extract() never raises -- it returns an
ExtractionResult with error_type for failures.

Worker recovery: if the background worker task dies (exception or
cancellation), it is automatically restarted on the next process() call
via _ensure_worker().
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from checkmates.models import NormalizedMessage
from checkmates.processors.base import MessageProcessor

if TYPE_CHECKING:
    from checkmates.database.repository import MessageRepository
    from checkmates.extraction.models import ExtractionResult
    from checkmates.extraction.service import ExtractionService

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_CAPACITY = 50
_DEFAULT_SHUTDOWN_TIMEOUT = 10.0


class ExtractionProcessor:
    """Processor that persists messages then enqueues AI extraction.

    Implements the MessageProcessor protocol.

    Wraps a PersistenceProcessor -- messages are persisted first, then
    enqueued for background Gemini extraction. This ensures no message
    is lost even if the queue is full or extraction fails.

    Args:
        wrapped: The underlying processor (typically PersistenceProcessor).
            Called first -- if it raises, the message is NOT enqueued
            and the collector will retry it on the next event.
        extraction_service: The ExtractionService for Gemini API calls.
        repository: Optional MessageRepository for persisting extracted
            tasks to the tasks table. When provided, actionable tasks
            from successful extractions are saved with INSERT OR IGNORE
            (dedup via unique index on source_message_id + title).
            When None (default), tasks are not persisted -- backward
            compatible with callers that don't need task persistence.
        queue_capacity: Maximum items in the extraction queue (default 50).
            When full, new messages are persisted but extraction is skipped
            with a warning log.
        shutdown_timeout: Seconds to wait for queue drain on shutdown
            (default 10.0). If the queue does not drain in time, the
            worker is cancelled.
    """

    def __init__(
        self,
        wrapped: MessageProcessor,
        extraction_service: ExtractionService,
        *,
        repository: MessageRepository | None = None,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._wrapped = wrapped
        self._extraction_service = extraction_service
        self._repository = repository
        self._queue_capacity = max(1, queue_capacity)
        self._shutdown_timeout = shutdown_timeout

        self._queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue(
            maxsize=self._queue_capacity
        )
        self._worker_task: asyncio.Task | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def queue_size(self) -> int:
        """Current number of items waiting in the extraction queue."""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        """True if the background worker task is currently active."""
        return self._worker_task is not None and not self._worker_task.done()

    # ------------------------------------------------------------------
    # MessageProcessor protocol
    # ------------------------------------------------------------------

    async def process(self, message: NormalizedMessage) -> None:
        """Persist the message, then enqueue for extraction.

        1. Call the wrapped processor (persist to DB). If it raises,
           the message is NOT enqueued -- the collector will retry it.
        2. If shutting down (_stopped), skip enqueuing. The message is
           already persisted and will not be lost.
        3. Ensure the background worker is running (lazy start, restart
           if crashed).
        4. Try to enqueue via put_nowait(). If the queue is full, log
           a warning with message_id/channel_id (no content) and skip
           extraction. The message remains persisted.
        """
        # Step 1: Persist first -- always, even during shutdown
        await self._wrapped.process(message)

        if self._stopped:
            logger.debug(
                "ExtractionProcessor stopped -- extraction skipped for "
                "message_id=%s (message persisted to DB)",
                message.message_id,
            )
            return

        # Step 2: Ensure the background worker is alive
        self._ensure_worker()

        # Step 3: Enqueue for extraction (non-blocking)
        try:
            self._queue.put_nowait(message)
            logger.debug(
                "Enqueued message_id=%s for extraction "
                "(queue_size=%d/%d)",
                message.message_id,
                self._queue.qsize(),
                self._queue_capacity,
            )
        except asyncio.QueueFull:
            logger.warning(
                "Extraction queue full (%d/%d) -- extraction SKIPPED for "
                "message_id=%s channel_id=%s. Message is persisted but "
                "will not be extracted. Consider increasing "
                "EXTRACTION_QUEUE_CAPACITY.",
                self._queue_capacity,
                self._queue_capacity,
                message.message_id,
                message.channel_id,
            )

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Start the worker if not running. Restart if it died.

        Called on every process() call. If the worker task is None
        or has completed (done()), create a new one. If it died with
        an exception, log it before restarting.
        """
        if self._worker_task is not None and not self._worker_task.done():
            return  # still running

        if self._worker_task is not None and self._worker_task.done():
            # Worker died -- log and restart
            try:
                exc = self._worker_task.exception()
                if exc:
                    logger.warning(
                        "Extraction worker died with exception: %s -- "
                        "restarting on next message",
                        exc,
                    )
                else:
                    logger.warning(
                        "Extraction worker exited unexpectedly -- "
                        "restarting on next message"
                    )
            except asyncio.CancelledError:
                logger.warning(
                    "Extraction worker was cancelled -- restarting "
                    "on next message"
                )

        self._worker_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        """Background worker: drain queue, call extraction service.

        Runs until cancelled. Each item is processed in a try/except --
        a failure in one message does not crash the worker.

        ExtractionService.extract() never raises (returns ExtractionResult
        with error_type), but we wrap in try/except as a safety net.

        When a repository is configured and extraction succeeds with
        actionable tasks, each task is persisted to the tasks table
        via repository.save_extracted_task(). Persistence failures are
        logged without crashing the worker.
        """
        logger.info("Extraction worker started")
        try:
            while True:
                message = await self._queue.get()
                try:
                    result = await self._extraction_service.extract(message)

                    if result.error_type is None:
                        logger.info(
                            "Extraction completed: message_id=%s "
                            "tasks=%d actionable=%s completions=%d "
                            "model=%s",
                            result.source_message_id,
                            len(result.tasks),
                            result.has_actionable_tasks,
                            len(result.completed_tasks),
                            result.model_used,
                        )
                        # Persist actionable tasks to the tasks table
                        if self._repository is not None:
                            await self._persist_tasks(
                                message, result
                            )
                            # Process task completions
                            if result.has_completions:
                                await self._process_completions(
                                    message, result
                                )
                    elif result.error_type == "skipped":
                        logger.debug(
                            "Extraction skipped: message_id=%s "
                            "reason=%s",
                            result.source_message_id,
                            result.skip_reason or result.error_type,
                        )
                    else:
                        logger.warning(
                            "Extraction failed: message_id=%s "
                            "error_type=%s",
                            result.source_message_id,
                            result.error_type,
                        )
                except Exception:
                    logger.exception(
                        "Unexpected error in extraction worker for "
                        "message_id=%s channel_id=%s",
                        message.message_id,
                        message.channel_id,
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("Extraction worker cancelled")
            raise

    async def _persist_tasks(
        self,
        message: NormalizedMessage,
        result: ExtractionResult,
    ) -> None:
        """Persist actionable extracted tasks to the tasks table.

        Called after a successful extraction (error_type is None).
        Each task is saved via repository.save_extracted_task() which
        uses INSERT OR IGNORE for database-level dedup.

        The Discord message ID is resolved to the internal messages.id
        via get_message_by_discord_id(). If the message is not found
        (should not happen — it was just persisted), the task is skipped
        with a warning.

        All exceptions are caught and logged — task persistence must
        never crash the extraction worker.
        """
        if not result.has_actionable_tasks or not result.tasks:
            return
        if result.context_project_id is None:
            return

        try:
            stored = await self._repository.get_message_by_discord_id(
                message.message_id
            )
            if stored is None:
                logger.warning(
                    "Cannot persist tasks: message discord_id=%s "
                    "not found in DB (should have been persisted first)",
                    message.message_id,
                )
                return

            saved_count = 0
            dup_count = 0
            for task in result.tasks:
                created = await self._repository.save_extracted_task(
                    project_id=result.context_project_id,
                    title=task.title,
                    description=task.description,
                    assignee_id=task.assignee_user_id,
                    deadline_text=task.deadline_text,
                    source_message_id=stored.id,
                )
                if created is not None:
                    saved_count += 1
                    logger.info(
                        "Task persisted: id=%s title=%s "
                        "source_message_id=%s",
                        created.id,
                        created.title,
                        stored.id,
                    )
                else:
                    dup_count += 1

            logger.info(
                "Task persistence complete for message_id=%s: "
                "%d saved, %d duplicates",
                message.message_id,
                saved_count,
                dup_count,
            )
        except Exception:
            logger.exception(
                "Failed to persist extracted tasks for "
                "message_id=%s channel_id=%s -- extraction result "
                "is lost, message remains persisted",
                message.message_id,
                message.channel_id,
            )

    async def _process_completions(
        self,
        message: NormalizedMessage,
        result: ExtractionResult,
    ) -> None:
        """Process task completions: match and mark existing tasks completed.

        Called after a successful extraction that identified completions.
        For each completion ref:
        1. Retrieve all tasks for the project.
        2. Filter to pending tasks only (don't re-complete).
        3. Match by title (case-insensitive exact, then substring).
        4. If exactly one match -> mark completed.
        5. If no match or multiple matches -> log diagnostic, no change.

        Project isolation is enforced by ``get_tasks_by_project`` which
        only returns tasks for the project associated with the message's
        channel (via ``result.context_project_id``).

        All exceptions are caught and logged — completion processing must
        never crash the extraction worker.
        """
        if not result.has_completions or not result.completed_tasks:
            return
        if result.context_project_id is None:
            return

        try:
            existing_tasks = await self._repository.get_tasks_by_project(
                result.context_project_id
            )

            completed_count = 0
            skipped_count = 0

            for comp in result.completed_tasks:
                # Only match against pending tasks
                pending = [t for t in existing_tasks if t.status == "pending"]

                if not pending:
                    logger.info(
                        "Completion: no pending tasks to match for "
                        "task_title=%r project_id=%s",
                        comp.task_title,
                        result.context_project_id,
                    )
                    skipped_count += 1
                    continue

                # Try exact case-insensitive match first
                matches = [
                    t for t in pending
                    if t.title.lower() == comp.task_title.lower()
                ]

                # If no exact match, try substring matching
                if not matches:
                    comp_lower = comp.task_title.lower()
                    matches = [
                        t for t in pending
                        if comp_lower in t.title.lower()
                        or t.title.lower() in comp_lower
                    ]

                if len(matches) == 1:
                    # Confident single match — mark completed
                    task = matches[0]
                    updated = await self._repository.update_task_status(
                        task.id, "completed"
                    )
                    if updated:
                        completed_count += 1
                        logger.info(
                            "Task completed: id=%s title=%s "
                            "matched_from=%r",
                            task.id,
                            task.title,
                            comp.task_title,
                        )
                    else:
                        # Was already completed between our query and update
                        logger.debug(
                            "Task already completed: id=%s title=%s",
                            task.id,
                            task.title,
                        )
                elif len(matches) == 0:
                    logger.info(
                        "Completion: no matching task for "
                        "task_title=%r (checked %d pending tasks)",
                        comp.task_title,
                        len(pending),
                    )
                    skipped_count += 1
                else:
                    # Multiple matches — ambiguous, don't complete
                    titles = [t.title for t in matches]
                    logger.info(
                        "Completion: ambiguous match for "
                        "task_title=%r — matched %d tasks: %s",
                        comp.task_title,
                        len(matches),
                        titles,
                    )
                    skipped_count += 1

            logger.info(
                "Completion processing complete for message_id=%s: "
                "%d completed, %d skipped",
                message.message_id,
                completed_count,
                skipped_count,
            )
        except Exception:
            logger.exception(
                "Failed to process completions for "
                "message_id=%s channel_id=%s",
                message.message_id,
                message.channel_id,
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Graceful shutdown: stop accepting work, drain, cancel.

        Idempotent: safe to call multiple times. The first call performs
        the drain-and-cancel sequence; subsequent calls return immediately.

        1. If already stopped, return immediately (idempotency guard).
        2. Set _stopped = True -- process() will still persist but
           skip enqueuing new extraction work.
        3. If no worker is running, return immediately.
        4. Wait for the queue to drain up to shutdown_timeout seconds.
        5. If timeout is exceeded, cancel the worker task.
        6. Await the worker task's completion, catching CancelledError
           and any unexpected exceptions to prevent unhandled task
           exception warnings.
        """
        # Idempotency: if already stopped (or shutting down), return.
        # This prevents double-drain, double-cancel, and races between
        # concurrent shutdown() calls.
        if self._stopped:
            return

        self._stopped = True
        logger.info(
            "ExtractionProcessor shutting down "
            "(queue_size=%d, timeout=%.1fs)",
            self._queue.qsize(),
            self._shutdown_timeout,
        )

        if self._worker_task is None:
            return

        if self._worker_task.done():
            return

        # Wait for the queue to drain or timeout
        try:
            await asyncio.wait_for(
                self._queue.join(),
                timeout=self._shutdown_timeout,
            )
            logger.info("Extraction queue drained successfully")
        except asyncio.TimeoutError:
            # qsize() counts items still in the queue. The worker may
            # additionally be processing one in-flight item that was
            # already get()-ed but not yet task_done()-ed.
            logger.warning(
                "Extraction queue drain timed out after %.1fs -- "
                "cancelling worker (%d items in queue, plus 1 in-flight "
                "item possible)",
                self._shutdown_timeout,
                self._queue.qsize(),
            )

        # Cancel the worker (it may be blocking on queue.get() or extract())
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # The worker died with an unexpected exception (not CancelledError).
            # Log it and continue -- we're shutting down anyway.
            logger.warning(
                "Extraction worker raised exception during shutdown: %s",
                exc,
            )

        logger.info("Extraction worker stopped")

    # ------------------------------------------------------------------
    # Testing helpers
    # ------------------------------------------------------------------

    async def wait_for_drain(self, timeout: float = 5.0) -> bool:
        """Wait for the queue to drain. For testing.

        Returns True if drained within timeout, False otherwise.
        """
        try:
            await asyncio.wait_for(
                self._queue.join(),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False
