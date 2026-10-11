"""Focused tests for automatic task completion detection.

Tests:
1. Task creation from a commitment (regression — existing behavior).
2. Assigning a new task to the message author.
3. Completing an existing task from a natural-language completion message.
4. Completion does not create a new pending task.
5. No matching task — completion is logged, no change.
6. Multiple possible matching tasks — ambiguous, no change.
7. Project isolation — completion in project A does not affect project B.
8. Repeated completion messages — idempotent, no duplicate changes.
9. Ordinary discussion and questions — no tasks created or completed.
10. Extraction API errors — message persisted, no task changes.
11. Malformed AI output — handled gracefully.
12. Existing extraction and persistence regression cases.

Uses MockGeminiClient — no live API calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from checkmates.context import ContextService
from checkmates.database import Database, MessageRepository
from checkmates.extraction import (
    AICompletedTask,
    AIExtractedTask,
    AITaskExtractionResponse,
    ExtractionService,
)
from checkmates.models import NormalizedMessage
from checkmates.processors.extraction import ExtractionProcessor
from checkmates.processors.persistence import PersistenceProcessor


# ---------------------------------------------------------------------------
# Mock Gemini client (reused pattern)
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
    """Mock google.genai.Client for testing."""

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
                completed_tasks=[],
                summary="No tasks found",
                has_actionable_tasks=False,
                has_completions=False,
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
    """Create a project with channels, members, and existing tasks."""
    project = await repo.create_project(server_id=3001, name="Hackathon Bot")
    await repo.add_project_channel(project.id, 2001, 3001)
    await repo.upsert_member(project.id, 1001, "Alice")
    await repo.upsert_member(project.id, 1002, "Bob")

    # Create existing tasks
    task1 = await repo.create_task(
        project.id, "Build API", description="REST endpoints",
        assignee_id=1001,
    )
    task2 = await repo.create_task(
        project.id, "Write tests", assignee_id=1002,
    )
    task3 = await repo.create_task(
        project.id, "Design UI", assignee_id=1001,
    )

    return {
        "project": project,
        "channel_id": 2001,
        "server_id": 3001,
        "task_ids": [task1.id, task2.id, task3.id],
        "task1": task1,
        "task2": task2,
        "task3": task3,
    }


@pytest.fixture
async def seeded_two_projects(repo: MessageRepository) -> dict:
    """Create two projects with tasks for isolation testing."""
    proj_a = await repo.create_project(server_id=3001, name="Alpha")
    await repo.add_project_channel(proj_a.id, 2001, 3001)
    await repo.upsert_member(proj_a.id, 1001, "Alice")
    task_a = await repo.create_task(
        proj_a.id, "Build API", assignee_id=1001,
    )

    proj_b = await repo.create_project(server_id=3001, name="Beta")
    await repo.add_project_channel(proj_b.id, 2002, 3001)
    await repo.upsert_member(proj_b.id, 1002, "Bob")
    task_b = await repo.create_task(
        proj_b.id, "Build API", assignee_id=1002,
    )

    return {
        "project_a": proj_a,
        "project_b": proj_b,
        "channel_a": 2001,
        "channel_b": 2002,
        "task_a": task_a,
        "task_b": task_b,
    }


@pytest.fixture
async def processor(
    repo: MessageRepository, extraction_service: ExtractionService
) -> ExtractionProcessor:
    """ExtractionProcessor with repository for task persistence + completion."""
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
# 1. Task creation from a commitment (regression)
# ---------------------------------------------------------------------------


class TestTaskCreationRegression:
    async def test_commitment_creates_pending_task(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """A commitment message must still create a pending task."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Deploy to production",
                    assignee_name="Alice",
                    confidence=0.9,
                ),
            ],
            completed_tasks=[],
            summary="New commitment",
            has_actionable_tasks=True,
            has_completions=False,
        )

        msg = _make_message("I'll deploy to production.", message_id=60001)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        # Original 3 + 1 new
        assert len(db_tasks) == 4
        deploy = [t for t in db_tasks if t.title == "Deploy to production"]
        assert len(deploy) == 1
        assert deploy[0].status == "pending"


# ---------------------------------------------------------------------------
# 2. Assigning a new task to the message author
# ---------------------------------------------------------------------------


class TestAuthorAssignment:
    async def test_task_assigned_to_message_author(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """When AI says assignee is the author, task gets author's user ID."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Review PR",
                    assignee_name="Alice",
                    confidence=0.9,
                ),
            ],
            completed_tasks=[],
            summary="Alice will review",
            has_actionable_tasks=True,
            has_completions=False,
        )

        # Alice (user 1001) is the message author
        msg = _make_message(
            "I'll review the PR.", message_id=60002, author_id=1001,
            author_name="Alice",
        )
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        review = [t for t in db_tasks if t.title == "Review PR"]
        assert len(review) == 1
        assert review[0].assignee_id == 1001  # Alice's discord_user_id


# ---------------------------------------------------------------------------
# 3. Completing an existing task
# ---------------------------------------------------------------------------


class TestTaskCompletion:
    async def test_completion_marks_task_completed(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """A completion message marks the matching task as completed."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Build API",
                    confidence=0.9,
                ),
            ],
            summary="Build API is done",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("I'm done with the Build API.", message_id=60003)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        task1 = seeded_project["task1"]
        updated = await repo.get_task(task1.id)
        assert updated.status == "completed"

    async def test_completion_by_substring_match(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Completion with partial title still matches via substring."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="API",
                    confidence=0.8,
                ),
            ],
            summary="API done",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("The API is done.", message_id=60004)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        task1 = seeded_project["task1"]
        updated = await repo.get_task(task1.id)
        assert updated.status == "completed"


# ---------------------------------------------------------------------------
# 4. Completion does not create a new task
# ---------------------------------------------------------------------------


class TestNoDuplicateOnCompletion:
    async def test_completion_does_not_create_new_task(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """A completion message must not create a new pending task."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Build API",
                    confidence=0.9,
                ),
            ],
            summary="Done with Build API",
            has_actionable_tasks=False,
            has_completions=True,
        )

        project = seeded_project["project"]
        tasks_before = await repo.get_tasks_by_project(project.id)
        count_before = len(tasks_before)

        msg = _make_message("I'm done with the Build API.", message_id=60005)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        tasks_after = await repo.get_tasks_by_project(project.id)
        assert len(tasks_after) == count_before  # no new task created


# ---------------------------------------------------------------------------
# 5. No matching task
# ---------------------------------------------------------------------------


class TestNoMatch:
    async def test_no_matching_task_no_change(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Completion for a non-existent task changes nothing."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Nonexistent task",
                    confidence=0.9,
                ),
            ],
            summary="Done with something",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("I'm done with the nonexistent task.", message_id=60006)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # All tasks should still be pending
        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        for t in db_tasks:
            assert t.status == "pending"


# ---------------------------------------------------------------------------
# 6. Multiple possible matching tasks (ambiguous)
# ---------------------------------------------------------------------------


class TestAmbiguousMatch:
    async def test_ambiguous_match_no_change(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        repo: MessageRepository,
    ) -> None:
        """When multiple tasks match, don't complete any."""
        project = await repo.create_project(server_id=3001, name="Ambig Test")
        await repo.add_project_channel(project.id, 2001, 3001)
        await repo.upsert_member(project.id, 1001, "Alice")

        # Two tasks with "API" in the title
        t1 = await repo.create_task(project.id, "Build API endpoint")
        t2 = await repo.create_task(project.id, "Test API endpoint")

        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="API",
                    confidence=0.9,
                ),
            ],
            summary="API done",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("The API is done.", message_id=60007)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Neither task should be completed
        assert (await repo.get_task(t1.id)).status == "pending"
        assert (await repo.get_task(t2.id)).status == "pending"


# ---------------------------------------------------------------------------
# 7. Project isolation
# ---------------------------------------------------------------------------


class TestProjectIsolation:
    async def test_completion_in_project_a_does_not_affect_project_b(
        self,
        repo: MessageRepository,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_two_projects: dict,
    ) -> None:
        """Completing a task in project A must not affect project B."""
        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            wrapped=persistence,
            extraction_service=extraction_service,
            repository=repo,
            queue_capacity=50,
            shutdown_timeout=5.0,
        )
        try:
            mock_client.parsed_response = AITaskExtractionResponse(
                tasks=[],
                completed_tasks=[
                    AICompletedTask(
                        task_title="Build API",
                        confidence=0.9,
                    ),
                ],
                summary="Done with Build API",
                has_actionable_tasks=False,
                has_completions=True,
            )

            # Message in project A's channel
            msg = _make_message(
                "I'm done with the Build API.",
                message_id=60008,
                channel_id=seeded_two_projects["channel_a"],
            )
            await proc.process(msg)
            await proc.wait_for_drain(timeout=2)

            # Project A's task should be completed
            task_a = seeded_two_projects["task_a"]
            assert (await repo.get_task(task_a.id)).status == "completed"

            # Project B's task must still be pending
            task_b = seeded_two_projects["task_b"]
            assert (await repo.get_task(task_b.id)).status == "pending"
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 8. Repeated completion messages (idempotent)
# ---------------------------------------------------------------------------


class TestRepeatedCompletion:
    async def test_repeated_completion_idempotent(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Processing the same completion twice doesn't cause issues."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Build API",
                    confidence=0.9,
                ),
            ],
            summary="Done",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("I'm done with the Build API.", message_id=60009)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        task1 = seeded_project["task1"]
        assert (await repo.get_task(task1.id)).status == "completed"

        # Process a second completion message (different message ID)
        msg2 = _make_message(
            "The Build API is complete.", message_id=60010
        )
        await processor.process(msg2)
        await processor.wait_for_drain(timeout=2)

        # Task should still be completed (not deleted, not errored)
        updated = await repo.get_task(task1.id)
        assert updated.status == "completed"

        # No new tasks should have been created
        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 3  # original count


# ---------------------------------------------------------------------------
# 9. Ordinary discussion and questions
# ---------------------------------------------------------------------------


class TestOrdinaryMessages:
    async def test_ordinary_discussion_no_changes(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Ordinary discussion creates no tasks and completes none."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[],
            summary="Just chatting",
            has_actionable_tasks=False,
            has_completions=False,
        )

        project = seeded_project["project"]
        tasks_before = await repo.get_tasks_by_project(project.id)
        count_before = len(tasks_before)
        statuses_before = {t.id: t.status for t in tasks_before}

        msg = _make_message("What does everyone think about the design?", message_id=60011)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        tasks_after = await repo.get_tasks_by_project(project.id)
        assert len(tasks_after) == count_before
        for t in tasks_after:
            assert t.status == statuses_before[t.id]


# ---------------------------------------------------------------------------
# 10. Extraction API errors
# ---------------------------------------------------------------------------


class TestApiErrors:
    async def test_api_error_message_persisted_no_task_changes(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """When the API fails, the message is still persisted and no tasks change."""
        mock_client.raise_exception = RuntimeError("Simulated API failure")

        project = seeded_project["project"]
        tasks_before = await repo.get_tasks_by_project(project.id)
        count_before = len(tasks_before)
        statuses_before = {t.id: t.status for t in tasks_before}

        msg = _make_message("I'm done with the Build API.", message_id=60012)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Message should still be persisted
        stored = await repo.get_message_by_discord_id(60012)
        assert stored is not None

        # No task changes
        tasks_after = await repo.get_tasks_by_project(project.id)
        assert len(tasks_after) == count_before
        for t in tasks_after:
            assert t.status == statuses_before[t.id]

        # Worker should still be running
        assert processor.is_running


# ---------------------------------------------------------------------------
# 11. Malformed AI output
# ---------------------------------------------------------------------------


class TestMalformedOutput:
    async def test_malformed_response_no_crash(
        self,
        repo: MessageRepository,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project: dict,
    ) -> None:
        """Malformed AI output is handled gracefully — no crash, no changes."""
        # Return a response with parsed=None to simulate parse failure
        mock_client.parsed_response = None

        # Override the mock to return a response with no parsed object
        original_generate = mock_client.aio.models.generate_content

        async def malformed_generate(*, model, contents, config):
            mock_client.call_count += 1
            return MockGenerateContentResponse(
                parsed=None,
                text=None,
                candidates=[MockCandidate(finish_reason="STOP")],
                prompt_feedback=MockPromptFeedback(),
            )

        mock_client.aio.models.generate_content = malformed_generate

        persistence = PersistenceProcessor(repo)
        proc = ExtractionProcessor(
            wrapped=persistence,
            extraction_service=extraction_service,
            repository=repo,
            queue_capacity=50,
            shutdown_timeout=5.0,
        )
        try:
            project = seeded_project["project"]
            tasks_before = await repo.get_tasks_by_project(project.id)
            count_before = len(tasks_before)

            msg = _make_message("I'm done with the Build API.", message_id=60013)
            await proc.process(msg)
            await proc.wait_for_drain(timeout=2)

            # No crash, no task changes
            tasks_after = await repo.get_tasks_by_project(project.id)
            assert len(tasks_after) == count_before
            for t in tasks_after:
                assert t.status == "pending"

            # Worker still running
            assert proc.is_running
        finally:
            await proc.shutdown()


# ---------------------------------------------------------------------------
# 12. Existing extraction and persistence regression
# ---------------------------------------------------------------------------


class TestRegression:
    async def test_existing_task_persistence_still_works(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Existing task persistence (create) still works alongside completions."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="New feature",
                    description="Build the new feature",
                    assignee_name="Bob",
                    confidence=0.9,
                ),
            ],
            completed_tasks=[
                AICompletedTask(
                    task_title="Build API",
                    confidence=0.9,
                ),
            ],
            summary="New task and a completion",
            has_actionable_tasks=True,
            has_completions=True,
        )

        msg = _make_message(
            "Bob will build the new feature. Also, I'm done with the Build API.",
            message_id=60014,
        )
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        project = seeded_project["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)

        # Original 3 + 1 new = 4
        assert len(db_tasks) == 4

        # Build API should be completed
        build_api = [t for t in db_tasks if t.title == "Build API"]
        assert len(build_api) == 1
        assert build_api[0].status == "completed"

        # New feature should be pending
        new_feature = [t for t in db_tasks if t.title == "New feature"]
        assert len(new_feature) == 1
        assert new_feature[0].status == "pending"
        assert new_feature[0].assignee_id == 1002  # Bob

    async def test_completion_does_not_delete_task(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """Completed tasks are not deleted — they remain visible."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Write tests",
                    confidence=0.9,
                ),
            ],
            summary="Tests done",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("I've finished the Write tests.", message_id=60015)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        task2 = seeded_project["task2"]
        updated = await repo.get_task(task2.id)
        # Task still exists, just marked completed
        assert updated is not None
        assert updated.status == "completed"

    async def test_already_completed_task_not_recompleted(
        self,
        processor: ExtractionProcessor,
        mock_client: MockGeminiClient,
        seeded_project: dict,
        repo: MessageRepository,
    ) -> None:
        """A task already marked completed is not affected by a new completion message."""
        # Pre-mark task1 as completed
        task1 = seeded_project["task1"]
        await repo.update_task_status(task1.id, "completed")

        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            completed_tasks=[
                AICompletedTask(
                    task_title="Build API",
                    confidence=0.9,
                ),
            ],
            summary="Done again",
            has_actionable_tasks=False,
            has_completions=True,
        )

        msg = _make_message("The Build API is complete.", message_id=60016)
        await processor.process(msg)
        await processor.wait_for_drain(timeout=2)

        # Task should still be completed (no error, no duplicate)
        updated = await repo.get_task(task1.id)
        assert updated.status == "completed"

        # Other tasks should still be pending
        task2 = seeded_project["task2"]
        assert (await repo.get_task(task2.id)).status == "pending"
