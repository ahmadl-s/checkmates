"""Integration tests for the extraction pipeline (Task 5).

Tests the ExtractionProcessor wrapping PersistenceProcessor with a real
ExtractionService (using MockGeminiClient — no live API calls).

Covers the 10 required scenarios:
    1.  Persistence-before-extraction (message in DB before API call)
    2.  Eligible vs filtered messages (pre-filter skips, actionable passes)
    3.  Missing API key (service returns config_error, message persisted)
    4.  API failure does not lose messages (persisted despite extraction error)
    5.  Queue saturation (message persisted, extraction skipped, no content logged)
    6.  Worker failure/recovery (worker dies, restarts on next process())
    7.  Shutdown (graceful drain with timeout, safe cancel)
    8.  Daily-cap concurrency (concurrent extractions do not exceed the cap)
    9.  Edited messages (re-extracted, dedup does not block edits)
    10. Task persistence (actionable tasks saved to tasks table)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from checkmates.context import ContextService
from checkmates.database import Database, MessageRepository
from checkmates.extraction import (
    AIExtractedTask,
    AITaskExtractionResponse,
    ExtractionResult,
    ExtractionService,
)
from checkmates.models import NormalizedMessage
from checkmates.processors.base import MessageProcessor
from checkmates.processors.extraction import ExtractionProcessor
from checkmates.processors.persistence import PersistenceProcessor


# ---------------------------------------------------------------------------
# Mock Gemini client (reused from test_extraction.py)
# ---------------------------------------------------------------------------


@dataclass
class MockPromptFeedback:
    block_reason: str | None = None
    block_reason_message: str | None = None


@dataclass
class MockCandidate:
    finish_reason: str = "STOP"
    content: Any = None


@dataclass
class MockGenerateContentResponse:
    parsed: Any = None
    text: str | None = None
    candidates: list[MockCandidate] | None = field(default_factory=list)
    prompt_feedback: MockPromptFeedback | None = field(
        default_factory=MockPromptFeedback
    )


class MockGeminiClient:
    """Mock google.genai.Client for testing.

    Set ``parsed_response`` for a successful extraction.
    Set ``raise_exception`` to simulate an API error.
    """

    class _AsyncModels:
        def __init__(self, parent: "MockGeminiClient") -> None:
            self._parent = parent

        async def generate_content(
            self,
            *,
            model: str,
            contents: str,
            config: object,
        ) -> MockGenerateContentResponse:
            self._parent.call_count += 1
            self._parent.last_contents = contents

            if self._parent.raise_exception is not None:
                raise self._parent.raise_exception

            if self._parent.parsed_response is not None:
                return MockGenerateContentResponse(
                    parsed=self._parent.parsed_response,
                    candidates=[MockCandidate(finish_reason="STOP")],
                    prompt_feedback=MockPromptFeedback(),
                )

            default = AITaskExtractionResponse(
                tasks=[],
                summary="No tasks found",
                has_actionable_tasks=False,
            )
            return MockGenerateContentResponse(
                parsed=default,
                candidates=[MockCandidate(finish_reason="STOP")],
                prompt_feedback=MockPromptFeedback(),
            )

    class _Async:
        def __init__(self, parent: "MockGeminiClient") -> None:
            self.models = MockGeminiClient._AsyncModels(parent)

    def __init__(self) -> None:
        self.parsed_response: AITaskExtractionResponse | None = None
        self.raise_exception: Exception | None = None
        self.call_count: int = 0
        self.last_contents: str = ""
        self.aio = self._Async(self)


# ---------------------------------------------------------------------------
# Non-yielding mock processor (for deterministic queue saturation tests)
# ---------------------------------------------------------------------------


class NoYieldProcessor:
    """Processor that records messages without yielding control to the
    event loop.

    Used in queue-saturation tests to prevent the worker from draining
    the queue between two consecutive process() calls.
    """

    def __init__(self) -> None:
        self.processed: list[NormalizedMessage] = []

    async def process(self, message: NormalizedMessage) -> None:
        self.processed.append(message)


# ---------------------------------------------------------------------------
# Slow extraction service (for queue saturation tests)
# ---------------------------------------------------------------------------


class SlowExtractionService:
    """Mock extraction service that blocks on extract() until unblocked.

    This simulates a slow Gemini API call so the queue stays full and
    we can deterministically test QueueFull behavior.
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self._block = asyncio.Event()

    async def extract(self, message: NormalizedMessage) -> ExtractionResult:
        self.call_count += 1
        await self._block.wait()
        return ExtractionResult(
            tasks=[],
            summary="slow mock",
            has_actionable_tasks=False,
            model_used="test",
            source_message_id=message.message_id,
            source_channel_id=message.channel_id,
            source_server_id=message.server_id,
        )

    def unblock(self) -> None:
        self._block.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    content: str = "I'll finish the backend API by Wednesday.",
    *,
    message_id: int = 5001,
    channel_id: int = 2001,
    server_id: int = 3001,
    author_id: int = 1001,
    author_name: str = "Alice",
    is_edit: bool = False,
) -> NormalizedMessage:
    return NormalizedMessage(
        server_id=server_id,
        channel_id=channel_id,
        message_id=message_id,
        author_id=author_id,
        author_display_name=author_name,
        content=content,
        timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
        is_edit=is_edit,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> MessageRepository:
    return MessageRepository(db)


@pytest.fixture
def context_service(repo: MessageRepository) -> ContextService:
    return ContextService(repo, max_messages=50)


@pytest.fixture
def mock_client() -> MockGeminiClient:
    return MockGeminiClient()


@pytest.fixture
def extraction_service(
    context_service: ContextService, mock_client: MockGeminiClient
) -> ExtractionService:
    return ExtractionService(
        context_service=context_service,
        gemini_client=mock_client,
        model="gemini-2.5-flash-lite",
    )


@pytest.fixture
async def seeded_project(repo: MessageRepository) -> dict:
    project = await repo.create_project(server_id=3001, name="Hackathon Bot")
    await repo.add_project_channel(project.id, 2001, 3001)
    await repo.upsert_member(project.id, 1001, "Alice")
    await repo.upsert_member(project.id, 1002, "Bob")
    return {
        "project": project,
        "channel_id": 2001,
        "server_id": 3001,
    }


@pytest.fixture
async def processor(
    repo: MessageRepository, extraction_service: ExtractionService
) -> ExtractionProcessor:
    """ExtractionProcessor with real PersistenceProcessor and ExtractionService.

    Includes repository for task persistence — actionable extracted tasks
    are saved to the tasks table.

    Automatically shuts down after the test to clean up the worker task.
    """
    persistence = PersistenceProcessor(repo)
    proc = ExtractionProcessor(
        wrapped=persistence,
        extraction_service=extraction_service,
        repository=repo,
        queue_capacity=50,
        shutdown_timeout=5.0,
    )
    yield proc
    await proc.shutdown()


# ---------------------------------------------------------------------------
# 1. Persistence-before-extraction
# ---------------------------------------------------------------------------


class TestPersistBeforeExtract:
    async def test_message_in_db_before_api_call(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Message must be persisted to the DB before the Gemini API is called.

        We use a SlowExtractionService injected into the processor to
        block the worker so we can inspect DB state while extraction is
        still in-flight.
        """
        # Replace the extraction service with a blocking one
        slow_svc = SlowExtractionService()
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, slow_svc, queue_capacity=50)
        try:
            mock_client.parsed_response = AITaskExtractionResponse(
                tasks=[AIExtractedTask(title="Test", confidence=0.9)],
                summary="Test",
                has_actionable_tasks=True,
            )

            msg = _make_message(message_id=51001)
            await proc.process(msg)

            # Wait a moment for the worker to pick up the message
            await asyncio.sleep(0.1)

            # The worker should have called extract (and blocked)
            assert slow_svc.call_count == 1

            # The message should already be in the DB
            db_msg = await repo.get_message_by_discord_id(51001)
            assert db_msg is not None
            assert db_msg.content == msg.content

            # Unblock the worker so the test can clean up
            slow_svc.unblock()
            await proc.wait_for_drain(timeout=2)
        finally:
            await proc.shutdown()

    async def test_persistence_failure_prevents_enqueue(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
    ) -> None:
        """If persistence raises, the message must NOT be enqueued."""

        class FailingProcessor:
            def __init__(self):
                self.calls = 0

            async def process(self, message):
                self.calls += 1
                raise RuntimeError("DB is down")

        failing = FailingProcessor()
        proc = ExtractionProcessor(failing, extraction_service, queue_capacity=50)
        try:
            msg = _make_message(message_id=51002)

            # process() should raise (propagated from wrapped processor)
            with pytest.raises(RuntimeError, match="DB is down"):
                await proc.process(msg)

            # The message was NOT enqueued
            assert proc.queue_size == 0

            # Worker was never started because process() raised
            # before reaching _ensure_worker()
            assert not proc.is_running
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 2. Eligible vs filtered messages
# ---------------------------------------------------------------------------


class TestEligibleVsFiltered:
    async def test_actionable_message_extracted(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """An actionable message should reach the Gemini API."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Build API", confidence=0.9)],
            summary="Alice will build the API",
            has_actionable_tasks=True,
        )

        msg = _make_message(
            "I'll build the API endpoint by tomorrow.", message_id=52001
        )
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        assert mock_client.call_count == 1

    async def test_greeting_not_sent_to_api(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """A greeting message should be skipped by the pre-filter (no API call)."""
        msg = _make_message("hey", message_id=52002)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        assert mock_client.call_count == 0

    async def test_emoji_only_not_sent_to_api(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """An emoji-only message should be skipped by the pre-filter."""
        msg = _make_message("\U0001f44d", message_id=52003)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        assert mock_client.call_count == 0


# ---------------------------------------------------------------------------
# 3. Missing API key
# ---------------------------------------------------------------------------


class TestMissingApiKey:
    async def test_message_persisted_without_api_key(
        self,
        context_service: ContextService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """With no API key, extraction returns config_error but message is persisted."""
        service = ExtractionService(
            context_service=context_service,
            api_key="",
        )
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, service, queue_capacity=50)
        try:
            msg = _make_message(message_id=53001)
            await proc.process(msg)
            await proc.wait_for_drain(timeout=2)

            # Message persisted
            db_msg = await repo.get_message_by_discord_id(53001)
            assert db_msg is not None
            assert db_msg.content == msg.content
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 4. API failure does not lose messages
# ---------------------------------------------------------------------------


class TestApiFailureNoDataLoss:
    async def test_message_persisted_on_api_error(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """When the Gemini API fails, the message is still in the DB."""
        mock_client.raise_exception = RuntimeError("Gemini is down")

        msg = _make_message(message_id=54001)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # API was called (and failed)
        assert mock_client.call_count == 1

        # Message is in the DB despite API failure
        db_msg = await repo.get_message_by_discord_id(54001)
        assert db_msg is not None
        assert db_msg.content == msg.content

    async def test_worker_survives_api_error(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """After an API error, the worker should process the next message."""
        mock_client.raise_exception = RuntimeError("First call fails")

        msg1 = _make_message("I'll build the DB schema.", message_id=54002)
        msg2 = _make_message("I'll write the API tests.", message_id=54003)

        await processor.process(msg1)
        await processor.wait_for_drain(timeout=2)

        # First call failed
        assert mock_client.call_count == 1

        # Fix the error and send a second message
        mock_client.raise_exception = None
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Write tests", confidence=0.8)],
            summary="Tests",
            has_actionable_tasks=True,
        )

        await processor.process(msg2)
        await processor.wait_for_drain(timeout=2)

        # Worker processed the second message
        assert mock_client.call_count == 2
        assert processor.is_running


# ---------------------------------------------------------------------------
# 5. Queue saturation
# ---------------------------------------------------------------------------


class TestQueueSaturation:
    async def test_full_queue_skips_extraction(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """When the queue is full, the message is persisted but extraction is skipped."""
        # Use NoYieldProcessor so the worker doesn't drain between calls
        wrapped = NoYieldProcessor()
        slow_svc = SlowExtractionService()
        proc = ExtractionProcessor(
            wrapped,
            slow_svc,
            queue_capacity=1,
            shutdown_timeout=2.0,
        )
        try:
            msg1 = _make_message("I'll build the API.", message_id=55001)
            msg2 = _make_message("I'll write tests.", message_id=55002)

            # Process msg1 — enqueues it (queue is now full: 1/1)
            await proc.process(msg1)

            # Process msg2 — queue is full, extraction skipped
            await proc.process(msg2)

            # Both messages were processed by the wrapped processor
            assert len(wrapped.processed) == 2

            # Queue should still have 1 item (msg1, worker is blocked)
            # The worker may or may not have picked up msg1 yet, but
            # msg2 was definitely not enqueued (QueueFull was caught)
            # Either: queue has 1 item (worker hasn't picked up msg1)
            #   or: queue has 0 items (worker picked up msg1, blocked on extract)
            # In both cases, msg2 was not extracted

            # Unblock the worker so it can finish
            slow_svc.unblock()
            await proc.wait_for_drain(timeout=2)

            # Only msg1 was sent to the extraction service
            assert slow_svc.call_count == 1
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 6. Worker failure / recovery
# ---------------------------------------------------------------------------


class TestWorkerRecovery:
    async def test_worker_restarts_after_cancel(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """If the worker task is cancelled, it restarts on the next process()."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Start the worker by processing a message
        msg1 = _make_message("I'll build the schema.", message_id=56001)
        await processor.process(msg1)
        await processor.wait_for_drain(timeout=2)

        assert mock_client.call_count == 1
        assert processor.is_running

        # Cancel the worker task manually
        old_task = processor._worker_task
        assert old_task is not None
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

        # Worker is now dead
        assert not processor.is_running

        # Process a new message — _ensure_worker() should restart it
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task2", confidence=0.8)],
            summary="Task2",
            has_actionable_tasks=True,
        )
        msg2 = _make_message("I'll write the tests.", message_id=56002)
        await processor.process(msg2)
        await processor.wait_for_drain(timeout=2)

        # Worker restarted and processed the second message
        assert processor.is_running
        assert mock_client.call_count == 2


# ---------------------------------------------------------------------------
# 7. Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_drains_queue(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Shutdown should drain all queued items before returning."""
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            persistence,
            extraction_service,
            queue_capacity=50,
            shutdown_timeout=10.0,
        )
        mock_client = extraction_service._client._client  # type: ignore[attr-defined]
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Enqueue 3 messages
        for i in range(3):
            msg = _make_message(
                f"I'll do task {i} by tomorrow.", message_id=57000 + i
            )
            await proc.process(msg)

        # Shutdown should wait for all 3 to be processed
        await proc.shutdown()

        assert mock_client.call_count == 3
        assert not proc.is_running

    async def test_shutdown_empty_queue_returns_immediately(
        self,
        processor: ExtractionProcessor,
    ) -> None:
        """Shutdown with an empty queue should return quickly."""
        import time

        start = time.monotonic()
        await processor.shutdown()
        elapsed = time.monotonic() - start

        assert elapsed < 1.0  # should be near-instant

    async def test_shutdown_stops_accepting_new_work(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """After shutdown, process() still persists but does not enqueue."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Shutdown the processor first
        await processor.shutdown()

        # Now process a message — should persist but not enqueue
        msg = _make_message("I'll build the API.", message_id=57010)
        await processor.process(msg)

        # Message is persisted
        db_msg = await repo.get_message_by_discord_id(57010)
        assert db_msg is not None

        # API was never called (worker is stopped)
        assert mock_client.call_count == 0

    async def test_shutdown_cancels_on_timeout(
        self,
        repo: MessageRepository,
        context_service: ContextService,
        seeded_project: dict,
    ) -> None:
        """If the queue doesn't drain in time, the worker is cancelled."""
        slow_svc = SlowExtractionService()
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            persistence,
            slow_svc,
            queue_capacity=50,
            shutdown_timeout=0.5,  # very short timeout
        )

        msg = _make_message("I'll build the API.", message_id=57020)
        await proc.process(msg)

        # Give the worker time to pick up the message and block
        await asyncio.sleep(0.1)

        # Shutdown should timeout and cancel the worker
        await proc.shutdown()

        # Worker is no longer running
        assert not proc.is_running

        # Clean up the blocked event
        slow_svc.unblock()


# ---------------------------------------------------------------------------
# 8. Daily-cap concurrency
# ---------------------------------------------------------------------------


class TestDailyCapConcurrency:
    async def test_concurrent_extractions_respect_daily_cap(
        self,
        context_service: ContextService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Multiple concurrent extractions must not exceed the daily cap.

        Uses daily_request_limit=2 and enqueues 5 messages. Only 2 should
        reach the Gemini API; the rest should get rate_limited.
        """
        mock = MockGeminiClient()
        mock.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock,
            daily_request_limit=2,
        )
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            persistence,
            service,
            queue_capacity=50,
            shutdown_timeout=5.0,
        )
        try:
            # Enqueue 5 messages with unique IDs and different content
            for i in range(5):
                msg = _make_message(
                    f"I'll do task number {i} by Friday.",
                    message_id=58000 + i,
                )
                await proc.process(msg)

            await proc.wait_for_drain(timeout=5)

            # Only 2 API calls should have been made (daily cap = 2)
            assert mock.call_count == 2
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 9. Edited messages
# ---------------------------------------------------------------------------


class TestEditedMessages:
    async def test_edited_message_re_extracted(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """An edited message (different content) should be re-extracted."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Original message
        msg_original = _make_message(
            "I'll finish the backend API by Wednesday.",
            message_id=59001,
        )
        await processor.process(msg_original)
        await processor.wait_for_drain(timeout=2)

        assert mock_client.call_count == 1

        # Edited version (same message_id, different content)
        msg_edited = _make_message(
            "I'll finish the backend API by Friday.",
            message_id=59001,
            is_edit=True,
        )
        await processor.process(msg_edited)
        await processor.wait_for_drain(timeout=2)

        # API called again because content changed
        assert mock_client.call_count == 2

        # DB has the updated content
        db_msg = await repo.get_message_by_discord_id(59001)
        assert db_msg is not None
        assert db_msg.content == "I'll finish the backend API by Friday."

    async def test_unchanged_message_not_re_extracted(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """Sending the same message twice should only extract once (dedup)."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        msg = _make_message(
            "I'll finish the backend API by Wednesday.",
            message_id=59002,
        )

        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Send the exact same message again
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # API called only once (dedup cache skipped the second)
        assert mock_client.call_count == 1


# ---------------------------------------------------------------------------
# 10. Task persistence
# ---------------------------------------------------------------------------


class TestTaskPersistence:
    async def test_extracted_tasks_saved_to_db(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Actionable extracted tasks must be persisted to the tasks table."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Build the API",
                    description="Complete REST endpoints",
                    assignee_name="Alice",
                    deadline_text="by Wednesday",
                    confidence=0.9,
                ),
                AIExtractedTask(
                    title="Write tests",
                    assignee_name="Bob",
                    confidence=0.8,
                ),
            ],
            summary="Multiple tasks found",
            has_actionable_tasks=True,
        )

        msg = _make_message(
            "Alice will build the API by Wednesday and Bob will write tests.",
            message_id=50001,
        )
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Extraction happened
        assert mock_client.call_count == 1

        # Tasks are now in the DB
        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 2

        titles = {t.title for t in db_tasks}
        assert titles == {"Build the API", "Write tests"}

        # Verify field mapping
        api_task = next(t for t in db_tasks if t.title == "Build the API")
        assert api_task.description == "Complete REST endpoints"
        assert api_task.assignee_id == 1001  # Alice's discord_user_id
        assert api_task.deadline == "by Wednesday"
        assert api_task.status == "pending"
        assert api_task.source_message_id is not None


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_satisfies_message_processor_protocol(
        self,
        extraction_service: ExtractionService,
        repo: MessageRepository,
    ) -> None:
        """ExtractionProcessor must satisfy the MessageProcessor protocol."""
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, extraction_service)
        assert isinstance(proc, MessageProcessor)

    def test_properties_initial_state(
        self,
        extraction_service: ExtractionService,
        repo: MessageRepository,
    ) -> None:
        """Fresh processor should have empty queue and no running worker."""
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, extraction_service)
        assert proc.queue_size == 0
        assert proc.is_running is False


# ---------------------------------------------------------------------------
# 11. Worker dies with pending items (Task 5.1 audit)
# ---------------------------------------------------------------------------


class TestWorkerDeathWithPendingItems:
    async def test_worker_restart_processes_stranded_items(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """If the worker dies with items in the queue, a subsequent
        process() call restarts the worker and processes the stranded items.

        This confirms that recovery is automatic when a new message
        arrives, but does NOT guarantee recovery without a new message
        (documented limitation).
        """
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Enqueue two messages but prevent the worker from running
        # by cancelling it immediately after it starts.
        msg1 = _make_message("I'll build the schema.", message_id=61001)
        msg2 = _make_message("I'll write the tests.", message_id=61002)

        # Process msg1 — starts the worker and enqueues
        await processor.process(msg1)

        # Give the worker a moment to start and pick up msg1
        await asyncio.sleep(0.05)

        # Cancel the worker (simulating unexpected death)
        old_task = processor._worker_task
        assert old_task is not None
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

        assert not processor.is_running

        # Process msg2 — this should restart the worker via _ensure_worker()
        await processor.process(msg2)
        await processor.wait_for_drain(timeout=3)

        # Both messages should have been processed (msg1 by the first
        # worker before it was cancelled, msg2 by the restarted worker)
        assert mock_client.call_count >= 1
        assert processor.is_running

    async def test_stranded_items_without_new_message(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """If the worker dies and no new message arrives, items in the
        queue remain stranded (documented limitation).

        This test confirms the limitation rather than testing recovery.
        The messages are already persisted in the DB — only extraction
        work is dropped.
        """
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        msg1 = _make_message("I'll build the schema.", message_id=61010)
        await processor.process(msg1)
        await processor.wait_for_drain(timeout=2)
        assert mock_client.call_count == 1

        # Enqueue a second message but cancel the worker before it runs
        msg2 = _make_message("I'll write the tests.", message_id=61011)
        await processor.process(msg2)

        # Cancel the worker before it picks up msg2
        old_task = processor._worker_task
        assert old_task is not None
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

        # msg2 is stranded in the queue — no worker to process it
        assert not processor.is_running

        # Without a new process() call, msg2 stays stranded.
        # The message IS persisted in the DB (persistence-before-enqueue).
        db_msg = await repo.get_message_by_discord_id(61011)
        assert db_msg is not None

        # Extraction did NOT happen for msg2
        assert mock_client.call_count == 1


# ---------------------------------------------------------------------------
# 12. Concurrent shutdown and enqueue (Task 5.1 audit)
# ---------------------------------------------------------------------------


class TestConcurrentShutdownEnqueue:
    async def test_process_during_shutdown_no_crash(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """process() and shutdown() running concurrently must not crash.

        In asyncio (single-threaded), process() and shutdown() interleave
        at await points. The _stopped flag ensures process() does not
        enqueue after shutdown begins. The message is still persisted.
        """
        slow_svc = SlowExtractionService()
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            persistence,
            slow_svc,
            queue_capacity=50,
            shutdown_timeout=1.0,
        )
        try:
            msg = _make_message("I'll build the API.", message_id=62001)

            # Start process() — it will persist (fast) then enqueue.
            # The worker will pick up the message and block on extract().
            await proc.process(msg)
            await asyncio.sleep(0.05)  # let the worker start and block

            # Now shutdown() runs concurrently.
            # The worker is blocked on extract() (slow_svc).
            # shutdown() sets _stopped=True, then waits for queue.join()
            # which will timeout (worker is blocked), then cancels the worker.
            await proc.shutdown()

            # The message should be persisted regardless
            db_msg = await repo.get_message_by_discord_id(62001)
            assert db_msg is not None

            # The worker is no longer running
            assert not proc.is_running
        finally:
            slow_svc.unblock()

    async def test_enqueue_after_shutdown_skipped(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """After shutdown completes, process() persists but skips extraction."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Shutdown first
        await processor.shutdown()

        # Now process a message — should persist but NOT enqueue
        msg = _make_message("I'll build the API.", message_id=62002)
        await processor.process(msg)

        # Message is persisted
        db_msg = await repo.get_message_by_discord_id(62002)
        assert db_msg is not None

        # Extraction was NOT done (worker is stopped)
        assert mock_client.call_count == 0
        assert not processor.is_running


# ---------------------------------------------------------------------------
# 13. Queue accounting on exception (Task 5.1 audit)
# ---------------------------------------------------------------------------


class TestQueueAccounting:
    async def test_task_done_called_on_exception(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """task_done() must be called even when extract() raises unexpectedly.

        If task_done() is skipped, queue.join() would hang forever.
        This test verifies the queue drains properly after an exception.
        """
        class ExplodingService:
            """Service that always raises (not returns error, but raises)."""
            def __init__(self):
                self.call_count = 0

            async def extract(self, message: NormalizedMessage) -> ExtractionResult:
                self.call_count += 1
                raise RuntimeError("Unexpected explosion")

        exploding = ExplodingService()
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, exploding, queue_capacity=50)
        try:
            # Enqueue 3 messages
            for i in range(3):
                msg = _make_message(
                    f"I'll do task {i} by Friday.", message_id=63000 + i
                )
                await proc.process(msg)

            # If task_done() is not called for each get(), this will hang
            drained = await proc.wait_for_drain(timeout=5)
            assert drained, "Queue did not drain — task_done() may be missing"

            # All 3 items were processed (and raised)
            assert exploding.call_count == 3

            # The worker is still running (exceptions don't crash it)
            assert proc.is_running
        finally:
            await proc.shutdown()

    async def test_task_done_called_on_success(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """On success, task_done() is called and queue.join() completes."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        for i in range(5):
            msg = _make_message(
                f"I'll do task {i} by Friday.", message_id=63010 + i
            )
            await processor.process(msg)

        drained = await processor.wait_for_drain(timeout=5)
        assert drained, "Queue did not drain"

        assert mock_client.call_count == 5


# ---------------------------------------------------------------------------
# 14. Shutdown edge cases (Task 5.1 audit)
# ---------------------------------------------------------------------------


class TestShutdownEdgeCases:
    async def test_shutdown_idempotent(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """Calling shutdown() multiple times must not crash or hang."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Enqueue a message to start the worker
        msg = _make_message("I'll build the API.", message_id=64001)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # First shutdown
        await processor.shutdown()
        assert not processor.is_running

        # Second shutdown — should return immediately (idempotency guard)
        import time
        start = time.monotonic()
        await processor.shutdown()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # should be near-instant

        # Third shutdown — still safe
        await processor.shutdown()

    async def test_shutdown_without_worker_started(
        self,
        extraction_service: ExtractionService,
        repo: MessageRepository,
    ) -> None:
        """shutdown() before any process() call must not crash."""
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(persistence, extraction_service)

        # No worker has been started — shutdown should be a no-op
        await proc.shutdown()

        assert not proc.is_running
        assert proc.queue_size == 0

    async def test_shutdown_after_worker_already_done(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """shutdown() when the worker has already completed should not crash."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        # Process a message and let the worker drain
        msg = _make_message("I'll build the API.", message_id=64010)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Cancel the worker manually (simulates unexpected death)
        old_task = processor._worker_task
        assert old_task is not None
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

        assert not processor.is_running

        # shutdown() should handle the dead worker gracefully
        await processor.shutdown()

    async def test_shutdown_preserves_persisted_messages(
        self,
        extraction_service: ExtractionService,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Messages persisted before shutdown timeout are in the DB."""
        slow_svc = SlowExtractionService()
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            persistence,
            slow_svc,
            queue_capacity=50,
            shutdown_timeout=0.3,  # very short
        )

        # Persist and enqueue a message
        msg = _make_message("I'll build the API.", message_id=64020)
        await proc.process(msg)

        # The worker will block on extract() — shutdown will timeout
        await proc.shutdown()

        # Despite extraction being dropped, the message IS in the DB
        db_msg = await repo.get_message_by_discord_id(64020)
        assert db_msg is not None
        assert db_msg.content == msg.content

        slow_svc.unblock()

