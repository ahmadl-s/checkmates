"""AI task extraction service using Google Gemini.

Extracts candidate tasks from Discord messages using the Gemini API
with structured outputs. The service:

1. Pre-filters messages deterministically (no AI call) to skip clearly
   irrelevant messages (greetings, acknowledgments, pure questions).
2. Retrieves project context via ContextService (preserves project/server
   isolation from Task 3.1).
3. Checks the application-level daily request cap before calling the API.
4. Checks the extraction dedup cache to avoid re-extracting unchanged
   messages. Edited messages produce a different content hash and are
   re-extracted.
5. Acquires a concurrency semaphore to prevent burst API calls.
6. Calls Gemini's generate_content() with response_schema for guaranteed
   structured output.
7. Handles API errors, rate limits, quota exhaustion, safety filtering,
   and parse failures gracefully — returns structured error results
   instead of raising.
8. Enriches AI output with source traceability and assignee resolution.

The service does NOT persist extracted tasks to the database. That is a
separate concern for a future task. Callers receive an ExtractionResult
and decide what to do with it.

The service does NOT fall back to OpenAI or any other provider. If the
Gemini API is unavailable, extraction fails gracefully with a structured
error.

Usage:
    service = ExtractionService(context_service, api_key="...", model="gemini-2.5-flash-lite")
    result = await service.extract(normalized_message)
    if result.error_type is None and result.has_actionable_tasks:
        for task in result.tasks:
            print(task.title, task.assignee_user_id)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Protocol, runtime_checkable

from checkmates.context import ContextService
from checkmates.context.models import ProjectContext
from checkmates.extraction.dedup import ExtractionDedupCache
from checkmates.extraction.models import (
    AICompletedTask,
    AIExtractedTask,
    AITaskExtractionResponse,
    CompletedTaskRef,
    ExtractedTask,
    ExtractionResult,
)
from checkmates.extraction.prefilter import ExtractionPrefilter
from checkmates.extraction.prompts import build_system_prompt, build_user_prompt
from checkmates.models import NormalizedMessage

logger = logging.getLogger(__name__)

# Default model for structured outputs (free-tier eligible)
_DEFAULT_MODEL = "gemini-2.5-flash-lite"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_CONCURRENT = 1
_DEFAULT_DAILY_LIMIT = 50


@runtime_checkable
class GeminiClientProtocol(Protocol):
    """Protocol for the Gemini client dependency.

    Allows injecting a mock client in tests without depending on the
    concrete google.genai.Client type. The only method used is
    ``aio.models.generate_content()``.
    """

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: object,
    ) -> object:
        """Call the Gemini generate_content endpoint.

        Returns a response object with ``.parsed`` (the validated Pydantic
        object or None), ``.text`` (the raw text or None), ``.candidates``
        (list or None), and ``.prompt_feedback`` (with ``.block_reason``).
        """
        ...


class _GeminiAdapter:
    """Thin adapter wrapping google.genai.Client to satisfy the protocol.

    This isolates the service from the concrete Gemini SDK type, making
    it trivially mockable. The adapter calls
    ``client.aio.models.generate_content()`` and returns the raw response.
    """

    def __init__(self, client) -> None:
        self._client = client

    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: object,
    ) -> object:
        return await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )


class ExtractionService:
    """Extracts candidate tasks from Discord messages using Gemini.

    The service is stateless except for the in-memory daily request
    counter and dedup cache. It is safe for concurrent use — the
    semaphore serializes API calls.

    Args:
        context_service: The project context retrieval service.
        api_key: Gemini API key. If empty and no gemini_client is provided,
            extraction returns a config_error result. The bot still starts.
        gemini_client: A pre-configured Gemini client (genai.Client or mock).
            If provided, api_key is ignored. Tests should always inject a mock.
        model: The Gemini model to use (default: gemini-2.5-flash-lite).
        timeout: Request timeout in seconds.
        max_concurrent: Maximum concurrent API requests (asyncio.Semaphore).
        daily_request_limit: Application-level daily request cap.
        prefilter: Optional ExtractionPrefilter. If None, a default one
            is created. Pass a custom one for testing.
        dedup_cache: Optional ExtractionDedupCache. If None, a default
            one is created.
    """

    def __init__(
        self,
        context_service: ContextService,
        *,
        api_key: str = "",
        gemini_client=None,
        model: str = _DEFAULT_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        daily_request_limit: int = _DEFAULT_DAILY_LIMIT,
        prefilter: ExtractionPrefilter | None = None,
        dedup_cache: ExtractionDedupCache | None = None,
    ) -> None:
        self._context_service = context_service
        self._model = model
        self._timeout = timeout
        self._max_concurrent = max(1, max_concurrent)
        self._daily_limit = max(0, daily_request_limit)
        self._prefilter = prefilter or ExtractionPrefilter()
        self._dedup = dedup_cache or ExtractionDedupCache()
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._daily_lock = asyncio.Lock()

        # Daily request tracking (in-memory, resets on restart or date change)
        self._daily_count = 0
        self._daily_reset_date: date | None = None

        # Initialize Gemini client
        if gemini_client is not None:
            self._client: GeminiClientProtocol | None = _GeminiAdapter(gemini_client)
        elif api_key:
            try:
                from google import genai

                client = genai.Client(api_key=api_key)
                self._client = _GeminiAdapter(client)
            except Exception as exc:
                logger.error("Failed to initialize Gemini client: %s", exc)
                self._client = None
        else:
            self._client = None
            logger.info(
                "ExtractionService initialized without an API key — "
                "extraction will return config_error until GEMINI_API_KEY is set"
            )

    @property
    def model(self) -> str:
        """The Gemini model used for extraction."""
        return self._model

    @property
    def daily_count(self) -> int:
        """Current daily request count (resets on date change in UTC)."""
        return self._daily_count

    async def extract(self, message: NormalizedMessage) -> ExtractionResult:
        """Extract candidate tasks from a message.

        Returns an ExtractionResult. Never raises — all errors are
        captured in the result's error and error_type fields.
        """
        # 1. Pre-filter: skip clearly irrelevant messages
        should_extract, skip_reason = self._prefilter.should_extract(message)
        if not should_extract:
            logger.info(
                "Extraction skipped by pre-filter: reason=%s "
                "message_id=%s channel=%s",
                skip_reason,
                message.message_id,
                message.channel_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Message skipped by pre-filter",
                has_actionable_tasks=False,
                model_used=self._model,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="Message was not sent to the AI model",
                error_type="skipped",
                skip_reason=skip_reason,
            )

        # 2. Retrieve project context (enforces server_id isolation)
        context = await self._context_service.get_context(message=message)

        if context.project is None:
            logger.info(
                "Extraction skipped: no project context for "
                "channel_id=%s server_id=%s message_id=%s",
                message.channel_id,
                message.server_id,
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="No project context available for this message",
                has_actionable_tasks=False,
                model_used=self._model,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="No project context available — channel is not registered "
                      "or server ID does not match",
                error_type="no_context",
            )

        # 3. Check if client is configured
        if self._client is None:
            logger.warning(
                "Extraction failed: GEMINI_API_KEY not configured for "
                "message_id=%s",
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Gemini API key not configured",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="GEMINI_API_KEY is not set — extraction is disabled",
                error_type="config_error",
            )

        # 4. Check daily request cap
        self._check_and_reset_daily_count()
        if self._daily_count >= self._daily_limit:
            logger.warning(
                "Extraction skipped: daily request limit (%d) reached for "
                "message_id=%s",
                self._daily_limit,
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Daily Gemini request limit reached",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error=f"Application-level daily request limit ({self._daily_limit}) reached. "
                      f"This is a courtesy cap — check your Google AI Studio quota for the "
                      f"server-side limit. The app cap resets at midnight UTC on restart.",
                error_type="rate_limited",
            )

        # 5. Check dedup — skip if this content was already extracted
        if self._dedup.is_extracted(message.message_id, message.content):
            logger.info(
                "Extraction skipped: already extracted (unchanged content) "
                "message_id=%s",
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Message already extracted (content unchanged)",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="This message was already extracted with identical content",
                error_type="skipped",
                skip_reason="already_extracted",
            )

        # 6. Build prompts
        system_prompt = build_system_prompt(context)
        user_prompt = build_user_prompt(message)

        # 7. Call Gemini with structured output (semaphore-limited)
        try:
            from google.genai import types as gemini_types

            config = gemini_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=AITaskExtractionResponse,
            )
        except Exception:
            # If google-genai is not installed, use a simple dict config
            config = {
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "response_schema": AITaskExtractionResponse,
            }

        # 7. Atomic check-and-increment of daily counter (race-safe)
        async with self._daily_lock:
            self._check_and_reset_daily_count()
            if self._daily_count >= self._daily_limit:
                logger.warning(
                    "Extraction skipped: daily request limit (%d) reached "
                    "for message_id=%s (race-safe check)",
                    self._daily_limit,
                    message.message_id,
                )
                return ExtractionResult(
                    tasks=[],
                    summary="Daily Gemini request limit reached",
                    has_actionable_tasks=False,
                    model_used=self._model,
                    context_project_id=context.project.id,
                    context_project_name=context.project.name,
                    source_message_id=message.message_id,
                    source_channel_id=message.channel_id,
                    source_server_id=message.server_id,
                    error=f"Application-level daily request limit ({self._daily_limit}) reached. "
                          f"This is a courtesy cap — check your Google AI Studio quota for the "
                          f"server-side limit. The app cap resets at midnight UTC on restart.",
                    error_type="rate_limited",
                )
            self._daily_count += 1

        # 8. Call Gemini with structured output (semaphore-limited)
        try:
            async with self._semaphore:
                response = await self._client.generate_content(
                    model=self._model,
                    contents=user_prompt,
                    config=config,
                )
        except Exception as exc:
            error_type, error_msg = self._classify_exception(exc)
            logger.warning(
                "Gemini API error for message_id=%s: type=%s msg=%s",
                message.message_id,
                error_type,
                error_msg,
            )
            return ExtractionResult(
                tasks=[],
                summary="Gemini API call failed",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error=error_msg,
                error_type=error_type,
            )

        # 8. Check for prompt-level safety blocking
        prompt_feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(prompt_feedback, "block_reason", None) if prompt_feedback else None
        if block_reason:
            block_msg = getattr(prompt_feedback, "block_reason_message", None) or str(block_reason)
            logger.warning(
                "Gemini safety blocked prompt for message_id=%s: %s",
                message.message_id,
                block_msg,
            )
            return ExtractionResult(
                tasks=[],
                summary="Gemini safety filter blocked the request",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error=f"Safety filter blocked the request: {block_msg}",
                error_type="safety_blocked",
            )

        # 9. Check for empty candidates
        candidates = getattr(response, "candidates", None)
        if not candidates:
            logger.warning(
                "Empty response from Gemini for message_id=%s",
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Gemini returned an empty response",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="Gemini response had no candidates",
                error_type="empty_response",
            )

        # 10. Check for candidate-level safety blocking
        finish_reason = getattr(candidates[0], "finish_reason", None)
        finish_reason_str = str(finish_reason) if finish_reason else ""
        if "SAFETY" in finish_reason_str or "PROHIBITED" in finish_reason_str:
            logger.warning(
                "Gemini safety blocked response for message_id=%s: finish_reason=%s",
                message.message_id,
                finish_reason_str,
            )
            return ExtractionResult(
                tasks=[],
                summary="Gemini safety filter blocked the response",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error=f"Response blocked by safety filter: {finish_reason_str}",
                error_type="safety_blocked",
            )

        # 11. Check for parse failure
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            # Try to get text and parse manually as fallback
            text = getattr(response, "text", None)
            if not text:
                logger.warning(
                    "Parse failure for message_id=%s — no parsed object and no text",
                    message.message_id,
                )
                return ExtractionResult(
                    tasks=[],
                    summary="Response could not be parsed into the expected schema",
                    has_actionable_tasks=False,
                    model_used=self._model,
                    context_project_id=context.project.id,
                    context_project_name=context.project.name,
                    source_message_id=message.message_id,
                    source_channel_id=message.channel_id,
                    source_server_id=message.server_id,
                    error="Failed to parse the structured response",
                    error_type="parse_error",
                )

        # Ensure parsed is the right type
        if parsed is not None and not isinstance(parsed, AITaskExtractionResponse):
            # SDK may return a dict — try to validate with Pydantic
            try:
                if isinstance(parsed, dict):
                    parsed = AITaskExtractionResponse.model_validate(parsed)
                elif isinstance(parsed, AITaskExtractionResponse):
                    pass
                else:
                    # Try to parse from text
                    text = getattr(response, "text", "")
                    parsed = AITaskExtractionResponse.model_validate_json(text)
            except Exception as exc:
                logger.warning(
                    "Parse failure for message_id=%s — response did not match schema: %s",
                    message.message_id,
                    exc,
                )
                return ExtractionResult(
                    tasks=[],
                    summary="Response did not match the expected schema",
                    has_actionable_tasks=False,
                    model_used=self._model,
                    context_project_id=context.project.id,
                    context_project_name=context.project.name,
                    source_message_id=message.message_id,
                    source_channel_id=message.channel_id,
                    source_server_id=message.server_id,
                    error=f"Response validation failed: {exc}",
                    error_type="parse_error",
                )

        if parsed is None:
            logger.warning(
                "Parse failure for message_id=%s — parsed is None after all checks",
                message.message_id,
            )
            return ExtractionResult(
                tasks=[],
                summary="Response could not be parsed into the expected schema",
                has_actionable_tasks=False,
                model_used=self._model,
                context_project_id=context.project.id,
                context_project_name=context.project.name,
                source_message_id=message.message_id,
                source_channel_id=message.channel_id,
                source_server_id=message.server_id,
                error="Failed to parse the structured response",
                error_type="parse_error",
            )

        # 12. Enrich AI output with source traceability and assignee resolution
        enriched_tasks = [
            self._enrich_task(task, message, context)
            for task in parsed.tasks
        ]

        # Enrich completions with source traceability
        enriched_completions = [
            self._enrich_completion(ct, message)
            for ct in parsed.completed_tasks
        ]

        # 13. Mark as extracted (only on success — failed requests are not marked)
        self._dedup.mark_extracted(message.message_id, message.content)

        logger.info(
            "Extraction complete for message_id=%s: %d tasks found, "
            "%d completions found, has_actionable_tasks=%s, has_completions=%s",
            message.message_id,
            len(enriched_tasks),
            len(enriched_completions),
            parsed.has_actionable_tasks,
            parsed.has_completions,
        )

        return ExtractionResult(
            tasks=enriched_tasks,
            completed_tasks=enriched_completions,
            summary=parsed.summary,
            has_actionable_tasks=parsed.has_actionable_tasks,
            has_completions=parsed.has_completions,
            model_used=self._model,
            context_project_id=context.project.id,
            context_project_name=context.project.name,
            source_message_id=message.message_id,
            source_channel_id=message.channel_id,
            source_server_id=message.server_id,
        )

    def _check_and_reset_daily_count(self) -> None:
        """Reset the daily count if the UTC date has changed."""
        today = datetime.now(timezone.utc).date()
        if self._daily_reset_date is None or self._daily_reset_date != today:
            self._daily_count = 0
            self._daily_reset_date = today

    def _classify_exception(self, exc: Exception) -> tuple[str, str]:
        """Classify a Gemini SDK exception into an error type.

        Returns (error_type, error_message).
        """
        # Check for google.genai.errors.ClientError / APIError
        exc_code = getattr(exc, "code", None)
        exc_status = getattr(exc, "status", "") or ""
        exc_message = getattr(exc, "message", "") or str(exc)

        if exc_code == 429 or "RESOURCE_EXHAUSTED" in exc_status.upper():
            return (
                "rate_limited",
                f"Gemini rate limit or quota exhausted: {exc_message}",
            )

        if exc_code in (401, 403) or "PERMISSION_DENIED" in exc_status.upper():
            return (
                "config_error",
                f"Gemini authentication failed: {exc_message}",
            )

        if exc_code == 400:
            return (
                "api_error",
                f"Gemini bad request: {exc_message}",
            )

        # TimeoutError or asyncio.TimeoutError
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return (
                "api_error",
                f"Gemini request timed out: {exc_message}",
            )

        # Default: generic API error
        return (
            "api_error",
            f"Gemini API error: {exc_message}",
        )

    def _enrich_task(
        self,
        ai_task: AIExtractedTask,
        message: NormalizedMessage,
        context: ProjectContext,
    ) -> ExtractedTask:
        """Enrich an AI-extracted task with source traceability and
        resolved assignee information.

        Assignee resolution: if the AI identified an assignee_name, try
        to match it against project members' display names. If matched,
        set assignee_user_id and assignee_confidence="high". If not
        matched, assignee_confidence="medium" (named but unresolvable).
        If no assignee was identified, assignee_confidence="none".
        """
        assignee_user_id: int | None = None
        assignee_confidence = "none"

        if ai_task.assignee_name:
            for member in context.members:
                member_name = member.display_name or ""
                if member_name.lower() == ai_task.assignee_name.lower():
                    assignee_user_id = member.discord_user_id
                    assignee_confidence = "high"
                    break

            if assignee_user_id is None:
                assignee_confidence = "medium"

        return ExtractedTask(
            title=ai_task.title,
            description=ai_task.description,
            assignee_user_id=assignee_user_id,
            assignee_display_name=ai_task.assignee_name,
            assignee_confidence=assignee_confidence,
            deadline_text=ai_task.deadline_text,
            confidence=ai_task.confidence,
            uncertainty_notes=ai_task.uncertainty_notes,
            source_message_id=message.message_id,
            source_channel_id=message.channel_id,
            source_server_id=message.server_id,
        )

    def _enrich_completion(
        self,
        ai_completion: AICompletedTask,
        message: NormalizedMessage,
    ) -> CompletedTaskRef:
        """Enrich an AI-identified completion with source traceability.

        Unlike task enrichment, no assignee resolution is needed —
        the completion is matched to an existing task by title in
        the ExtractionProcessor.
        """
        return CompletedTaskRef(
            task_title=ai_completion.task_title,
            confidence=ai_completion.confidence,
            uncertainty_notes=ai_completion.uncertainty_notes,
            source_message_id=message.message_id,
            source_channel_id=message.channel_id,
            source_server_id=message.server_id,
        )
