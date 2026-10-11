"""Tests for AI task extraction with Gemini (Task 4.1).

All tests use a mock Gemini client — no live API calls are made.

Covers:
    A. Valid extraction with clear tasks
    B. No-task messages (casual conversation)
    C. Ambiguous fields (unclear assignee, no deadline)
    D. Assignee resolution (name matched to project member)
    E. Invalid / unparseable API response
    F. API failure (timeout, auth error, rate limit)
    G. Safety filtering (prompt blocked, response blocked)
    H. No project context (unregistered channel / server mismatch)
    I. Source traceability verification
    J. Regression: context service still works, no DB writes, prompts
    K. Configuration error (no API key)
    L. Daily request cap and quota exhaustion
    M. Dedup: unchanged messages and edited messages
    N. Pre-filter integration
    O. No automatic provider fallback
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from checkmates.context import ContextService
from checkmates.database import Database, MessageRepository
from checkmates.extraction import (
    AIExtractedTask,
    AITaskExtractionResponse,
    ExtractionDedupCache,
    ExtractionResult,
    ExtractionService,
)
from checkmates.models import NormalizedMessage


# ---------------------------------------------------------------------------
# Mock Gemini client
# ---------------------------------------------------------------------------


@dataclass
class MockPromptFeedback:
    """Mimics google.genai.types.GenerateContentResponsePromptFeedback."""
    block_reason: str | None = None
    block_reason_message: str | None = None


@dataclass
class MockCandidate:
    """Mimics google.genai.types.Candidate."""
    finish_reason: str = "STOP"
    content: Any = None


@dataclass
class MockGenerateContentResponse:
    """Mimics google.genai.types.GenerateContentResponse."""
    parsed: Any = None
    text: str | None = None
    candidates: list[MockCandidate] | None = field(default_factory=list)
    prompt_feedback: MockPromptFeedback | None = field(default_factory=MockPromptFeedback)


class MockGeminiClient:
    """Mock google.genai.Client for testing.

    Mirrors the real SDK structure: ``client.aio.models.generate_content(...)``.
    No live API calls are made.

    Set ``parsed_response`` to the AITaskExtractionResponse that should
    be returned. Set ``raise_exception`` to simulate an API error. Set
    ``empty_candidates`` to simulate an empty response. Set ``block_reason``
    to simulate a prompt-level safety block. Set ``safety_finish_reason``
    to simulate a candidate-level safety block. Set ``parsed_is_none``
    to simulate a parse failure.
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
            self._parent.last_config = config

            if self._parent.raise_exception is not None:
                raise self._parent.raise_exception

            # Prompt-level safety block
            if self._parent.block_reason is not None:
                return MockGenerateContentResponse(
                    parsed=None,
                    text=None,
                    candidates=None,
                    prompt_feedback=MockPromptFeedback(
                        block_reason=self._parent.block_reason,
                        block_reason_message=self._parent.block_reason,
                    ),
                )

            # Empty candidates
            if self._parent.empty_candidates:
                return MockGenerateContentResponse(
                    parsed=None,
                    text=None,
                    candidates=[],
                    prompt_feedback=MockPromptFeedback(),
                )

            # Candidate-level safety block
            if self._parent.safety_finish_reason:
                return MockGenerateContentResponse(
                    parsed=None,
                    text=None,
                    candidates=[MockCandidate(finish_reason="SAFETY")],
                    prompt_feedback=MockPromptFeedback(),
                )

            # Parse failure (parsed is None, text is None)
            if self._parent.parsed_is_none:
                return MockGenerateContentResponse(
                    parsed=None,
                    text=None,
                    candidates=[MockCandidate(finish_reason="STOP")],
                    prompt_feedback=MockPromptFeedback(),
                )

            # Valid response
            if self._parent.parsed_response is not None:
                return MockGenerateContentResponse(
                    parsed=self._parent.parsed_response,
                    text=None,
                    candidates=[MockCandidate(finish_reason="STOP")],
                    prompt_feedback=MockPromptFeedback(),
                )

            # Default: no tasks
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
        self.empty_candidates: bool = False
        self.block_reason: str | None = None
        self.safety_finish_reason: bool = False
        self.parsed_is_none: bool = False
        self.call_count: int = 0
        self.last_contents: str = ""
        self.last_config: object = None
        self.aio = self._Async(self)


def _make_mock_client(
    parsed: AITaskExtractionResponse | None = None,
    *,
    raise_exc: Exception | None = None,
    empty: bool = False,
    block_reason: str | None = None,
    safety: bool = False,
    parsed_none: bool = False,
) -> MockGeminiClient:
    client = MockGeminiClient()
    client.parsed_response = parsed
    client.raise_exception = raise_exc
    client.empty_candidates = empty
    client.block_reason = block_reason
    client.safety_finish_reason = safety
    client.parsed_is_none = parsed_none
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory database for testing."""
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
    """ExtractionService with a mock Gemini client."""
    return ExtractionService(
        context_service=context_service,
        gemini_client=mock_client,
        model="gemini-2.5-flash-lite",
    )


@pytest.fixture
async def seeded_project_with_member(repo: MessageRepository) -> dict:
    """Create a project with a registered channel and member."""
    project = await repo.create_project(server_id=3001, name="Hackathon Bot")
    await repo.add_project_channel(project.id, 2001, 3001)
    await repo.upsert_member(project.id, 1001, "Alice")
    await repo.upsert_member(project.id, 1002, "Bob")

    return {
        "project": project,
        "channel_id": 2001,
        "server_id": 3001,
        "member_ids": [1001, 1002],
    }


def _make_message(
    content: str = "I'll finish the backend API by Wednesday.",
    *,
    message_id: int = 5001,
    channel_id: int = 2001,
    server_id: int = 3001,
    author_id: int = 1001,
    author_name: str = "Alice",
) -> NormalizedMessage:
    return NormalizedMessage(
        server_id=server_id,
        channel_id=channel_id,
        message_id=message_id,
        author_id=author_id,
        author_display_name=author_name,
        content=content,
        timestamp=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# A. Valid extraction with clear tasks
# ---------------------------------------------------------------------------


class TestValidExtraction:
    async def test_extracts_single_task(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A message with a clear task should produce one ExtractedTask."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Finish backend API",
                    description="Complete the REST endpoints",
                    assignee_name="Alice",
                    deadline_text="by Wednesday",
                    confidence=0.9,
                    uncertainty_notes=None,
                ),
            ],
            summary="Alice committed to finishing the backend API by Wednesday",
            has_actionable_tasks=True,
        )

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type is None
        assert result.error is None
        assert result.has_actionable_tasks is True
        assert len(result.tasks) == 1
        assert result.tasks[0].title == "Finish backend API"
        assert result.tasks[0].description == "Complete the REST endpoints"
        assert result.tasks[0].confidence == 0.9
        assert result.tasks[0].deadline_text == "by Wednesday"

    async def test_extracts_multiple_tasks(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A message mentioning multiple tasks should produce multiple results."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Build database schema",
                    confidence=0.85,
                ),
                AIExtractedTask(
                    title="Write API tests",
                    assignee_name="Bob",
                    confidence=0.8,
                ),
            ],
            summary="Multiple tasks discussed",
            has_actionable_tasks=True,
        )

        msg = _make_message("We need to build the DB schema and Bob will write tests.")
        result = await extraction_service.extract(msg)

        assert result.has_actionable_tasks is True
        assert len(result.tasks) == 2
        titles = {t.title for t in result.tasks}
        assert "Build database schema" in titles
        assert "Write API tests" in titles

    async def test_result_carries_project_info(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """ExtractionResult should carry project ID and name."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="No tasks",
            has_actionable_tasks=False,
        )

        msg = _make_message()
        result = await extraction_service.extract(msg)

        assert result.context_project_id == seeded_project_with_member["project"].id
        assert result.context_project_name == "Hackathon Bot"
        assert result.model_used == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# B. No-task messages (casual conversation)
# ---------------------------------------------------------------------------


class TestNoTaskMessages:
    async def test_casual_message_returns_no_tasks(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A casual message with no actionable tasks should return empty tasks."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="Casual greeting, no tasks",
            has_actionable_tasks=False,
        )

        msg = _make_message("I'll work on the project tomorrow, anyone have ideas?")
        result = await extraction_service.extract(msg)

        assert result.error_type is None
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0
        assert result.summary == "Casual greeting, no tasks"

    async def test_status_update_without_commitment_returns_no_tasks(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A status update without a new commitment is not a task."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="Status update, no new tasks",
            has_actionable_tasks=False,
        )

        msg = _make_message("I finished the API and deployed it yesterday.")
        result = await extraction_service.extract(msg)

        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0


# ---------------------------------------------------------------------------
# C. Ambiguous fields (unclear assignee, no deadline)
# ---------------------------------------------------------------------------


class TestAmbiguousFields:
    async def test_unclear_assignee_notes_uncertainty(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When the AI is uncertain about the assignee, it should note it."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Set up CI/CD",
                    description=None,
                    assignee_name=None,
                    deadline_text=None,
                    confidence=0.6,
                    uncertainty_notes="Unclear who is responsible for this task",
                ),
            ],
            summary="Task mentioned but assignee unclear",
            has_actionable_tasks=True,
        )

        msg = _make_message("Someone should set up CI/CD for the project.")
        result = await extraction_service.extract(msg)

        assert result.has_actionable_tasks is True
        assert len(result.tasks) == 1
        assert result.tasks[0].uncertainty_notes == "Unclear who is responsible for this task"
        assert result.tasks[0].assignee_user_id is None
        assert result.tasks[0].assignee_confidence == "none"

    async def test_no_deadline_is_not_invented(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When no deadline is mentioned, deadline_text should be None."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Write documentation",
                    assignee_name="Alice",
                    deadline_text=None,
                    confidence=0.8,
                    uncertainty_notes=None,
                ),
            ],
            summary="Alice will write docs",
            has_actionable_tasks=True,
        )

        msg = _make_message("I'll write the documentation for the project.")
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1
        assert result.tasks[0].deadline_text is None


# ---------------------------------------------------------------------------
# D. Assignee resolution (name matched to project member)
# ---------------------------------------------------------------------------


class TestAssigneeResolution:
    async def test_assignee_matched_to_member(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """An assignee name matching a project member should resolve to user_id."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Build frontend",
                    assignee_name="Alice",
                    confidence=0.9,
                ),
            ],
            summary="Alice assigned to frontend",
            has_actionable_tasks=True,
        )

        msg = _make_message("Alice will build the frontend.")
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1
        assert result.tasks[0].assignee_user_id == 1001
        assert result.tasks[0].assignee_display_name == "Alice"
        assert result.tasks[0].assignee_confidence == "high"

    async def test_assignee_not_matched_to_member(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """An assignee name not matching any member should have confidence 'medium'."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Design logo",
                    assignee_name="Charlie",
                    confidence=0.7,
                ),
            ],
            summary="Charlie assigned to logo",
            has_actionable_tasks=True,
        )

        msg = _make_message("Charlie will design the logo for us.")
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1
        assert result.tasks[0].assignee_user_id is None
        assert result.tasks[0].assignee_display_name == "Charlie"
        assert result.tasks[0].assignee_confidence == "medium"

    async def test_no_assignee_means_none_confidence(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """No assignee mentioned should yield confidence 'none'."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Deploy to production",
                    assignee_name=None,
                    confidence=0.7,
                ),
            ],
            summary="Deployment needed",
            has_actionable_tasks=True,
        )

        msg = _make_message("We need to deploy the project to production.")
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1
        assert result.tasks[0].assignee_user_id is None
        assert result.tasks[0].assignee_display_name is None
        assert result.tasks[0].assignee_confidence == "none"

    async def test_assignee_match_case_insensitive(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """Assignee matching should be case-insensitive."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(
                    title="Review PR",
                    assignee_name="alice",
                    confidence=0.9,
                ),
            ],
            summary="alice to review",
            has_actionable_tasks=True,
        )

        msg = _make_message("alice should review the pull request.")
        result = await extraction_service.extract(msg)

        assert result.tasks[0].assignee_user_id == 1001
        assert result.tasks[0].assignee_confidence == "high"


# ---------------------------------------------------------------------------
# E. Invalid / unparseable API response
# ---------------------------------------------------------------------------


class TestInvalidResponse:
    async def test_parse_failure_returns_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When parsed is None, return a parse_error result."""
        mock_client.parsed_is_none = True

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "parse_error"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0

    async def test_empty_candidates_returns_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When the API returns no candidates, return an empty_response error."""
        mock_client.empty_candidates = True

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "empty_response"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0


# ---------------------------------------------------------------------------
# F. API failure (timeout, auth error, rate limit)
# ---------------------------------------------------------------------------


class TestAPIFailure:
    async def test_api_timeout_returns_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A timeout exception should return an api_error result."""
        mock_client.raise_exception = TimeoutError("Request timed out")

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "api_error"
        assert "timed out" in result.error.lower()
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0

    async def test_api_auth_error_returns_config_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A 401 authentication error should return a config_error result."""
        # Create a mock ClientError-like exception
        class MockClientError(Exception):
            def __init__(self, code, status, message):
                self.code = code
                self.status = status
                self.message = message
                super().__init__(message)

        mock_client.raise_exception = MockClientError(401, "UNAUTHENTICATED", "API key not valid")

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "config_error"
        assert "auth" in result.error.lower()
        assert result.has_actionable_tasks is False

    async def test_api_error_preserves_project_info(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """Even on API error, project context info should be preserved."""
        mock_client.raise_exception = RuntimeError("Internal server error")

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "api_error"
        assert result.context_project_id == seeded_project_with_member["project"].id
        assert result.context_project_name == "Hackathon Bot"


# ---------------------------------------------------------------------------
# G. Safety filtering (prompt blocked, response blocked)
# ---------------------------------------------------------------------------


class TestSafetyBlocked:
    async def test_prompt_blocked_returns_safety_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When Gemini blocks the prompt, return a safety_blocked result."""
        mock_client.block_reason = "SAFETY"

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "safety_blocked"
        assert "safety" in result.error.lower()
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0
        assert result.context_project_id is not None

    async def test_response_blocked_returns_safety_error(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """When Gemini blocks the response at candidate level, return safety_blocked."""
        mock_client.safety_finish_reason = True

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "safety_blocked"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0


# ---------------------------------------------------------------------------
# H. No project context (unregistered channel / server mismatch)
# ---------------------------------------------------------------------------


class TestNoProjectContext:
    async def test_unregistered_channel_returns_no_context(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
    ) -> None:
        """A message from an unregistered channel should return no_context."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Test", confidence=0.5)],
            summary="Test",
            has_actionable_tasks=True,
        )

        msg = _make_message(channel_id=99999)
        result = await extraction_service.extract(msg)

        assert result.error_type == "no_context"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0
        assert mock_client.call_count == 0  # API should not be called

    async def test_server_mismatch_returns_no_context(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A message with mismatched server_id should return no_context."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Test", confidence=0.5)],
            summary="Test",
            has_actionable_tasks=True,
        )

        msg = _make_message(server_id=9999)
        result = await extraction_service.extract(msg)

        assert result.error_type == "no_context"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0
        assert mock_client.call_count == 0  # API should not be called


# ---------------------------------------------------------------------------
# I. Source traceability verification
# ---------------------------------------------------------------------------


class TestSourceTraceability:
    async def test_task_carries_source_message_id(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """ExtractedTask should carry the source message's Discord ID."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="Test task", confidence=0.8),
            ],
            summary="Test",
            has_actionable_tasks=True,
        )

        msg = _make_message(message_id=12345)
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1
        assert result.tasks[0].source_message_id == 12345

    async def test_task_carries_source_channel_and_server(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """ExtractedTask should carry source channel and server IDs."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Test", confidence=0.8)],
            summary="Test",
            has_actionable_tasks=True,
        )

        msg = _make_message(channel_id=2001, server_id=3001)
        result = await extraction_service.extract(msg)

        assert result.tasks[0].source_channel_id == 2001
        assert result.tasks[0].source_server_id == 3001

    async def test_result_carries_source_ids(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """ExtractionResult should carry source message/channel/server IDs."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="No tasks",
            has_actionable_tasks=False,
        )

        msg = _make_message(message_id=77777, channel_id=2001, server_id=3001)
        result = await extraction_service.extract(msg)

        assert result.source_message_id == 77777
        assert result.source_channel_id == 2001
        assert result.source_server_id == 3001


# ---------------------------------------------------------------------------
# J. Regression: context service still works, no DB writes, prompts
# ---------------------------------------------------------------------------


class TestRegressionWithExtraction:
    async def test_context_service_unchanged(
        self,
        context_service: ContextService,
        seeded_project_with_member: dict,
    ) -> None:
        """ContextService should still work independently of extraction."""
        ctx = await context_service.get_context(channel_id=2001)
        assert ctx.project is not None
        assert ctx.project.name == "Hackathon Bot"

    async def test_extraction_does_not_persist(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
        repo: MessageRepository,
    ) -> None:
        """Extraction should not write any tasks to the database."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[
                AIExtractedTask(title="New task", confidence=0.9),
            ],
            summary="New task",
            has_actionable_tasks=True,
        )

        msg = _make_message()
        result = await extraction_service.extract(msg)

        assert len(result.tasks) == 1

        project = seeded_project_with_member["project"]
        db_tasks = await repo.get_tasks_by_project(project.id)
        assert len(db_tasks) == 0

    async def test_prompts_include_project_context(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """The system prompt should include project name and member names."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="No tasks",
            has_actionable_tasks=False,
        )

        msg = _make_message()
        await extraction_service.extract(msg)

        assert mock_client.call_count == 1
        # last_contents is the user prompt — system instruction is in config
        assert "Hackathon Bot" in str(mock_client.last_config) or "Hackathon Bot" in mock_client.last_contents


# ---------------------------------------------------------------------------
# K. Configuration error (no API key)
# ---------------------------------------------------------------------------


class TestConfigError:
    async def test_no_api_key_returns_config_error(
        self,
        context_service: ContextService,
        seeded_project_with_member: dict,
    ) -> None:
        """When no API key is set, extraction should return config_error."""
        service = ExtractionService(
            context_service=context_service,
            api_key="",
            model="gemini-2.5-flash-lite",
        )

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await service.extract(msg)

        assert result.error_type == "config_error"
        assert "GEMINI_API_KEY" in result.error
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0

    async def test_bot_starts_without_api_key(
        self,
        context_service: ContextService,
    ) -> None:
        """ExtractionService should be constructible without an API key."""
        service = ExtractionService(
            context_service=context_service,
            api_key="",
        )
        # Should not raise — just returns config_error on extract()
        assert service.model == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# L. Daily request cap and quota exhaustion
# ---------------------------------------------------------------------------


class TestRateLimited:
    async def test_rate_limit_429_returns_rate_limited(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A 429 error should return a rate_limited result."""
        class MockClientError(Exception):
            def __init__(self, code, status, message):
                self.code = code
                self.status = status
                self.message = message
                super().__init__(message)

        mock_client.raise_exception = MockClientError(
            429, "RESOURCE_EXHAUSTED", "Quota exceeded"
        )

        msg = _make_message("I'll finish the backend API by Wednesday.")
        result = await extraction_service.extract(msg)

        assert result.error_type == "rate_limited"
        assert "rate limit" in result.error.lower() or "quota" in result.error.lower()
        assert result.has_actionable_tasks is False

    async def test_daily_cap_returns_rate_limited(
        self,
        context_service: ContextService,
        seeded_project_with_member: dict,
    ) -> None:
        """When daily request limit is reached, return rate_limited."""
        mock = MockGeminiClient()
        mock.parsed_response = AITaskExtractionResponse(
            tasks=[],
            summary="No tasks",
            has_actionable_tasks=False,
        )

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock,
            daily_request_limit=2,
        )

        # Use unique message IDs and different content to avoid dedup
        msg1 = _make_message("I'll finish the backend by today.", message_id=60001)
        msg2 = _make_message("I need to write the tests by tomorrow.", message_id=60002)
        msg3 = _make_message("We should deploy the project by Friday.", message_id=60003)

        r1 = await service.extract(msg1)
        r2 = await service.extract(msg2)
        r3 = await service.extract(msg3)

        assert r1.error_type is None
        assert r2.error_type is None
        assert r3.error_type == "rate_limited"
        assert "daily request limit" in r3.error.lower()


# ---------------------------------------------------------------------------
# M. Dedup: unchanged messages and edited messages
# ---------------------------------------------------------------------------


class TestDedup:
    async def test_unchanged_message_skipped(
        self,
        context_service: ContextService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """Re-extracting an unchanged message should be skipped."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock_client,
        )

        msg = _make_message("I'll finish the backend API by Wednesday.", message_id=70001)
        r1 = await service.extract(msg)
        r2 = await service.extract(msg)  # Same content

        assert r1.error_type is None
        assert r1.has_actionable_tasks is True
        assert r2.error_type == "skipped"
        assert r2.skip_reason == "already_extracted"
        assert mock_client.call_count == 1  # API called only once

    async def test_edited_message_re_extracted(
        self,
        context_service: ContextService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """An edited message (different content) should be re-extracted."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock_client,
        )

        msg_original = _make_message(
            "I'll finish the backend API by Wednesday.", message_id=70002
        )
        msg_edited = _make_message(
            "I'll finish the backend API by Friday.", message_id=70002
        )

        r1 = await service.extract(msg_original)
        r2 = await service.extract(msg_edited)  # Different content

        assert r1.error_type is None
        assert r2.error_type is None  # Re-extracted, not skipped
        assert mock_client.call_count == 2  # API called twice

    async def test_failed_api_not_marked(
        self,
        context_service: ContextService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A failed API request should NOT be marked as extracted."""
        mock_client.raise_exception = RuntimeError("Server error")

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock_client,
        )

        msg = _make_message("I'll finish the backend by Wednesday.", message_id=70003)
        r1 = await service.extract(msg)

        assert r1.error_type == "api_error"

        # Now fix the error and retry
        mock_client.raise_exception = None
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Task", confidence=0.8)],
            summary="Task",
            has_actionable_tasks=True,
        )
        r2 = await service.extract(msg)  # Same content — should NOT be skipped

        assert r2.error_type is None  # Successfully extracted
        assert r2.has_actionable_tasks is True
        assert mock_client.call_count == 2  # API called twice (retry)


# ---------------------------------------------------------------------------
# N. Pre-filter integration
# ---------------------------------------------------------------------------


class TestPreFilterIntegration:
    async def test_greeting_skipped_by_prefilter(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """A greeting message should be skipped by the pre-filter."""
        msg = _make_message("hey", message_id=80001)
        result = await extraction_service.extract(msg)

        assert result.error_type == "skipped"
        assert result.skip_reason == "greeting"
        assert mock_client.call_count == 0  # API not called

    async def test_actionable_message_passes_prefilter(
        self,
        extraction_service: ExtractionService,
        mock_client: MockGeminiClient,
        seeded_project_with_member: dict,
    ) -> None:
        """An actionable message should pass the pre-filter and reach the API."""
        mock_client.parsed_response = AITaskExtractionResponse(
            tasks=[AIExtractedTask(title="Build API", confidence=0.9)],
            summary="Task",
            has_actionable_tasks=True,
        )

        msg = _make_message("I'll build the API endpoint by tomorrow.", message_id=80002)
        result = await extraction_service.extract(msg)

        assert result.error_type is None
        assert result.has_actionable_tasks is True
        assert mock_client.call_count == 1


# ---------------------------------------------------------------------------
# O. No automatic provider fallback
# ---------------------------------------------------------------------------


class TestNoFallback:
    async def test_no_openai_import_in_service(
        self,
    ) -> None:
        """The service module should not import or use the OpenAI SDK."""
        import checkmates.extraction.service as svc_module
        source = open(svc_module.__file__).read()
        source_lower = source.lower()
        # Check for actual OpenAI SDK imports/usage, not mentions in comments
        assert "import openai" not in source_lower, (
            "OpenAI import found in service.py — provider fallback is prohibited"
        )
        assert "from openai" not in source_lower, (
            "OpenAI import found in service.py — provider fallback is prohibited"
        )
        assert "asyncopenai" not in source_lower, (
            "AsyncOpenAI usage found in service.py — provider fallback is prohibited"
        )
        assert "openai(" not in source_lower, (
            "OpenAI constructor call found in service.py — provider fallback is prohibited"
        )

    async def test_no_fallback_on_gemini_failure(
        self,
        context_service: ContextService,
        seeded_project_with_member: dict,
    ) -> None:
        """When Gemini fails, the service should return an error, not fall back."""
        mock = MockGeminiClient()
        mock.raise_exception = RuntimeError("Gemini is down")

        service = ExtractionService(
            context_service=context_service,
            gemini_client=mock,
        )

        msg = _make_message("I'll finish the backend API by Wednesday.", message_id=90001)
        result = await service.extract(msg)

        # Should return an error, not attempt any fallback
        assert result.error_type == "api_error"
        assert result.has_actionable_tasks is False
        assert len(result.tasks) == 0
