"""Focused tests for extracted task persistence to SQLite.

Tests that actionable extracted tasks are saved to the tasks table,
that duplicates are prevented at the database level, that optional
fields (assignee, deadline) are handled safely, and that database
failures do not crash the extraction worker.

Uses MockGeminiClient — no live Gemini API calls.
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
from checkmates.processors.extraction import ExtractionProcessor
from checkmates.processors.persistence import PersistenceProcessor


# ---------------------------------------------------------------------------
# Mock Gemini client (reused from test_extraction_pipeline.py)
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
        self.aio = self._Async(self)


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
    """ExtractionProcessor with repository for task persistence."""
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
# 1. Successful task persistence
# ---------------------------------------------------------------------------


class TestSuccessfulPersistence:
    async def test_tasks_saved_to_db(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Actionable tasks must be persisted to the tasks table."""
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
                    description=None,
                    assignee_name="Bob",
                    deadline_text=None,
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

        assert mock_client.call_count == 1

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 2

    async def test_source_message_traceability(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """source_message_id must point to the internal messages.id."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Do the thing", confidence=0.9),
            ],
            summary="One task",
            has_actionable_tasks=True,
        )

        msg = _make_message("Do the thing", message_id=50002)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # The message was persisted
        stored = await repo.get_message_by_discord_id(50002)
        assert stored is not None

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        assert db_tasks[0].source_message_id == stored.id

    async def test_assignee_resolved_to_discord_id(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Assignee name from AI must be resolved to Discord user ID."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Build frontend",
                    assignee_name="Alice",
                    confidence=0.9,
                ),
            ],
            summary="One task",
            has_actionable_tasks=True,
        )

        msg = _make_message("Alice will build the frontend.", message_id=50003)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        assert db_tasks[0].assignee_id == 1001  # Alice's discord_user_id

    async def test_no_tasks_when_extraction_returns_none(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """When AI finds no tasks, none are persisted."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="No tasks found",
            has_actionable_tasks=False,
        )

        msg = _make_message("Just chatting about the weather.", message_id=50004)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 0


# ---------------------------------------------------------------------------
# 2. Duplicate processing
# ---------------------------------------------------------------------------


class TestDuplicatePrevention:
    async def test_same_message_same_title_no_duplicate(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Re-processing the same message must not create duplicate tasks.

        The extraction dedup cache prevents re-extraction in normal
        operation, but the database unique index is the authoritative
        guarantee. We simulate by calling save_extracted_task twice
        with the same source_message_id and title.
        """
        project = seeded_project["project"]

        # Insert a message to get its internal ID
        msg = _make_message("Build the API", message_id=50010)
        await repo.insert_message(msg)
        stored = await repo.get_message_by_discord_id(50010)
        assert stored is not None

        # Save the same task twice
        first = await repo.save_extracted_task(
            project_id=project.id,
            title="Build the API",
            description="REST endpoints",
            assignee_id=1001,
            deadline_text="by Friday",
            source_message_id=stored.id,
        )
        second = await repo.save_extracted_task(
            project_id=project.id,
            title="Build the API",
            description="REST endpoints",
            assignee_id=1001,
            deadline_text="by Friday",
            source_message_id=stored.id,
        )

        assert first is not None
        assert second is None  # duplicate, ignored

        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1

    async def test_different_titles_same_message_both_saved(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Same message with different task titles creates separate tasks."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Build API", confidence=0.9),
                AIExtractedTask(title="Write docs", confidence=0.8),
            ],
            summary="Two tasks",
            has_actionable_tasks=True,
        )

        msg = _make_message("Build API and write docs.", message_id=50011)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 2
        titles = {t.title for t in db_tasks}
        assert titles == {"Build API", "Write docs"}

    async def test_reprocessed_message_no_new_tasks(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """If the same message is processed twice, no duplicate tasks appear.

        The extraction dedup cache prevents re-extraction, but even if
        it's bypassed, the DB unique index prevents duplicate rows.
        """
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Build the API", confidence=0.9),
            ],
            summary="One task",
            has_actionable_tasks=True,
        )

        msg = _make_message("Build the API.", message_id=50012)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Process the same message again (dedup cache should skip
        # the AI call, but even if it didn't, the DB prevents dupes)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1


# ---------------------------------------------------------------------------
# 3. Optional fields (absent assignee, absent deadline)
# ---------------------------------------------------------------------------


class TestOptionalFields:
    async def test_no_assignee(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Task without an assignee is saved with assignee_id=NULL."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Deploy to staging",
                    confidence=0.7,
                ),
            ],
            summary="One task, no assignee",
            has_actionable_tasks=True,
        )

        msg = _make_message("Someone should deploy to staging.", message_id=50020)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        assert db_tasks[0].assignee_id is None
        assert db_tasks[0].deadline is None

    async def test_unresolved_assignee(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Assignee named by AI but not matched to a member → assignee_id=NULL."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Review PR",
                    assignee_name="Charlie",
                    confidence=0.8,
                ),
            ],
            summary="One task, unresolvable assignee",
            has_actionable_tasks=True,
        )

        msg = _make_message("Charlie should review the PR.", message_id=50021)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        # Charlie is not a registered member → assignee_id is None
        assert db_tasks[0].assignee_id is None

    async def test_deadline_text_stored_as_text(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Deadline text from AI is stored directly in the TEXT column."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Finish report",
                    deadline_text="by end of next week",
                    confidence=0.9,
                ),
            ],
            summary="One task with deadline",
            has_actionable_tasks=True,
        )

        msg = _make_message("Finish the report by end of next week.", message_id=50022)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        assert db_tasks[0].deadline == "by end of next week"

    async def test_no_description(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Task without a description is saved with description=NULL."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Fix the bug", confidence=0.9),
            ],
            summary="One task, no description",
            has_actionable_tasks=True,
        )

        msg = _make_message("Fix the bug.", message_id=50023)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 1
        assert db_tasks[0].description is None


# ---------------------------------------------------------------------------
# 4. Database failure handling
# ---------------------------------------------------------------------------


class TestDatabaseFailure:
    async def test_persistence_failure_does_not_crash_worker(
        self,
        repo: MessageRepository,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """If save_extracted_task raises, the worker must not crash."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Build the API", confidence=0.9),
            ],
            summary="One task",
            has_actionable_tasks=True,
        )

        # Create a processor with a repo that raises on save_extracted_task
        class FailingRepo:
            def __init__(self, real_repo: MessageRepository) -> None:
                self._real = real_repo

            async def get_message_by_discord_id(self, discord_id):
                return await self._real.get_message_by_discord_id(discord_id)

            async def save_extracted_task(self, *args, **kwargs):
                raise RuntimeError("Simulated DB failure")

        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            wrapped=persistence,
            extraction_service=extraction_service,
            repository=FailingRepo(repo),  # type: ignore[arg-type]
            queue_capacity=50,
            shutdown_timeout=5.0,
        )
        try:
            msg = _make_message("Build the API.", message_id=50030)
            await proc.process(msg)
            await proc.wait_for_drain(timeout=2)

            # Worker should still be running
            assert proc.is_running

            # Message should still be persisted
            stored = await repo.get_message_by_discord_id(50030)
            assert stored is not None
        finally:
            await proc.shutdown()

    async def test_worker_continues_after_persistence_error(
        self,
        repo: MessageRepository,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """After a persistence failure, the next message's tasks are saved."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Build the API", confidence=0.9),
            ],
            summary="One task",
            has_actionable_tasks=True,
        )

        call_count = 0

        class FlakyRepo:
            def __init__(self, real_repo: MessageRepository) -> None:
                self._real = real_repo
                self._fail = True

            async def get_message_by_discord_id(self, discord_id):
                return await self._real.get_message_by_discord_id(discord_id)

            async def save_extracted_task(self, *args, **kwargs):
                if self._fail:
                    self._fail = False
                    raise RuntimeError("First call fails")
                return await self._real.save_extracted_task(*args, **kwargs)

        persistence = PersistenceProcessor(repo)
        flaky = FlakyRepo(repo)
        proc = ExtractionProcessor(
            wrapped=persistence,
            extraction_service=extraction_service,
            repository=flaky,  # type: ignore[arg-type]
            queue_capacity=50,
            shutdown_timeout=5.0,
        )
        try:
            # First message: persistence fails, but worker survives
            msg1 = _make_message("Build the API.", message_id=50031)
            await proc.process(msg1)
            await proc.wait_for_drain(timeout=2)

            # Second message: persistence should succeed
            mock_client.parsed_response = AITaskExtractionResponse(
                tasks=[
                    AIExtractedTask(title="Write docs", confidence=0.8),
                ],
                summary="One task",
                has_actionable_tasks=True,
            )
            msg2 = _make_message("Write the docs.", message_id=50032)
            await proc.process(msg2)
            await proc.wait_for_drain(timeout=2)

            # Worker still running
            assert proc.is_running

            # Second task was persisted
            project = seeded_project["project"]
            db_tasks = await repo.get_tasks_by_project(project.id)
            assert len(db_tasks) == 1
            assert db_tasks[0].title == "Write docs"
        finally:
            await proc.shutdown()
