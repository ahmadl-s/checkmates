"""Pydantic models for AI task extraction.

Two layers of models:

1. **AI response models** (AITaskExtractionResponse, AIExtractedTask):
   Used as the ``response_format`` for ``openai.AsyncOpenAI.chat.completions.parse()``.
   The OpenAI API guarantees the response JSON conforms to this schema.
   These models contain only what the AI can infer from the message text
   and project context — no source-message metadata (the AI doesn't know
   Discord message IDs).

2. **Service result models** (ExtractionResult, ExtractedTask):
   Returned by ExtractionService to the caller. These enrich the AI
   output with source traceability (message ID, channel ID, server ID)
   and resolved assignee information (Discord user ID matched from
   project members). They also carry error information when extraction
   fails.

This separation keeps the AI response schema clean (no source IDs the
AI cannot know) while giving callers rich, traceable results.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# AI Response Models (used as response_format for OpenAI parse())
# ---------------------------------------------------------------------------


class AIExtractedTask(BaseModel):
    """A single task candidate as identified by the AI.

    This is what the OpenAI structured output returns for each task.
    Fields are populated only when the AI finds evidence in the message.
    """

    title: str = Field(
        description="A concise, actionable task title (imperative mood, e.g., 'Build API endpoint')"
    )
    description: str | None = Field(
        default=None,
        description="Additional details or context about the task, if available in the message",
    )
    assignee_name: str | None = Field(
        default=None,
        description="The name of the person assigned to this task, as mentioned in the message. "
        "Must be explicitly stated — do not guess or invent.",
    )
    deadline_text: str | None = Field(
        default=None,
        description="The deadline as expressed in the message text (e.g., 'by Friday', 'next Wednesday'). "
        "Must be explicitly stated — do not guess or invent.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0 to 1.0) that this is a genuine actionable task",
    )
    uncertainty_notes: str | None = Field(
        default=None,
        description="Notes about missing, ambiguous, or uncertain information. "
        "Populate when assignee, deadline, or scope is unclear.",
    )


class AICompletedTask(BaseModel):
    """A task that the AI identifies as completed.

    The AI returns this when a message clearly states that an existing
    task has been finished. The ``task_title`` is used to match against
    existing project tasks.
    """

    task_title: str = Field(
        description="The title of the task that was completed. "
        "Match this against the Existing Tasks list when possible."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0 to 1.0) that this is a genuine completion statement",
    )
    uncertainty_notes: str | None = Field(
        default=None,
        description="Notes about ambiguity, e.g., if it's unclear which task was completed.",
    )


class AITaskExtractionResponse(BaseModel):
    """Top-level structured response from the OpenAI API.

    Passed as response_format to client.chat.completions.parse().
    The API guarantees the model output conforms to this schema.
    """

    tasks: list[AIExtractedTask] = Field(
        default_factory=list,
        description="New tasks to CREATE from this message. Empty if no new commitments found.",
    )
    completed_tasks: list[AICompletedTask] = Field(
        default_factory=list,
        description="Existing tasks to mark COMPLETED based on this message. "
        "Only include when the message clearly states a task is done. "
        "Match task_title against the Existing Tasks list.",
    )
    summary: str = Field(
        description="A brief one-sentence summary of what the message is about"
    )
    has_actionable_tasks: bool = Field(
        description="True if at least one new actionable task was identified in the message"
    )
    has_completions: bool = Field(
        default=False,
        description="True if at least one task completion was identified in the message",
    )


# ---------------------------------------------------------------------------
# Service Result Models (returned by ExtractionService)
# ---------------------------------------------------------------------------


class ExtractedTask(BaseModel):
    """A candidate task extracted from a message, enriched with project context.

    This is the caller-facing model. It carries source traceability
    (which message, channel, server it came from) and resolved
    assignee information (Discord user ID matched from project members).
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(description="Concise, actionable task title")
    description: str | None = Field(
        default=None,
        description="Additional details about the task",
    )
    assignee_user_id: int | None = Field(
        default=None,
        description="Resolved Discord user ID of the assignee, matched from project members. "
        "None if no assignee was identified or if the name couldn't be matched.",
    )
    assignee_display_name: str | None = Field(
        default=None,
        description="Display name of the assignee as mentioned in the message",
    )
    assignee_confidence: Literal["high", "medium", "low", "none"] = Field(
        default="none",
        description="Confidence that the assignee was correctly identified. "
        "'high' = explicitly named and matched to a project member. "
        "'medium' = named but not matched. "
        "'low' = ambiguous reference. "
        "'none' = no assignee mentioned.",
    )
    deadline_text: str | None = Field(
        default=None,
        description="Deadline as expressed in the message text (unparsed)",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="AI confidence score that this is a genuine actionable task",
    )
    uncertainty_notes: str | None = Field(
        default=None,
        description="Notes about missing, ambiguous, or uncertain information",
    )
    source_message_id: int = Field(
        description="Discord message ID this task was extracted from"
    )
    source_channel_id: int = Field(
        description="Discord channel ID of the source message"
    )
    source_server_id: int = Field(
        description="Discord server ID of the source message"
    )


class CompletedTaskRef(BaseModel):
    """A reference to a task that should be marked completed.

    Enriched with source traceability (which message, channel, server).
    The ``task_title`` is used by the ExtractionProcessor to match
    against existing project tasks.
    """

    model_config = ConfigDict(frozen=True)

    task_title: str = Field(
        description="Title of the task to complete (for matching against existing tasks)"
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="AI confidence that this is a genuine completion statement",
    )
    uncertainty_notes: str | None = Field(
        default=None,
        description="Notes about ambiguity",
    )
    source_message_id: int = Field(
        description="Discord message ID this completion was extracted from"
    )
    source_channel_id: int = Field(
        description="Discord channel ID of the source message"
    )
    source_server_id: int = Field(
        description="Discord server ID of the source message"
    )


# Error type constants for ExtractionResult.error_type
ErrorType = Literal[
    "no_context",       # no project found for the message's channel/server
    "config_error",     # API key not configured or client initialization failed
    "api_error",        # API call failed (network, auth, bad request)
    "rate_limited",     # 429 rate limit or daily request cap exceeded
    "parse_error",      # response could not be parsed into the schema
    "safety_blocked",   # Gemini safety filter blocked the prompt or response
    "empty_response",   # response had no candidates or empty content
    "skipped",          # pre-filter determined the message is not actionable
]


class ExtractionResult(BaseModel):
    """Full result of an AI extraction operation.

    On success: tasks is populated, error is None.
    On failure: tasks is empty, error and error_type describe the failure.

    This model is the single return type of ExtractionService.extract(),
    regardless of whether extraction succeeded or failed. Callers should
    check error_type is None before using tasks.
    """

    model_config = ConfigDict(frozen=True)

    tasks: list[ExtractedTask] = Field(
        default_factory=list,
        description="Candidate tasks extracted from the message (empty on error or no tasks)",
    )
    completed_tasks: list[CompletedTaskRef] = Field(
        default_factory=list,
        description="Task completions extracted from the message (empty if none)",
    )
    summary: str = Field(
        default="",
        description="Brief summary of the message or error description",
    )
    has_actionable_tasks: bool = Field(
        default=False,
        description="True if at least one actionable task was identified",
    )
    has_completions: bool = Field(
        default=False,
        description="True if at least one task completion was identified",
    )
    model_used: str = Field(
        description="The OpenAI model used for this extraction",
    )
    context_project_id: int | None = Field(
        default=None,
        description="Project ID from the resolved context, if a project was found",
    )
    context_project_name: str | None = Field(
        default=None,
        description="Project name from the resolved context, if a project was found",
    )
    source_message_id: int = Field(
        description="Discord message ID of the message that was analyzed"
    )
    source_channel_id: int = Field(
        description="Discord channel ID of the source message"
    )
    source_server_id: int = Field(
        description="Discord server ID of the source message"
    )
    error: str | None = Field(
        default=None,
        description="Error message if extraction failed, None on success",
    )
    error_type: ErrorType | None = Field(
        default=None,
        description="Error type categorization. None on success.",
    )
    skip_reason: str | None = Field(
        default=None,
        description="Reason the message was skipped by the pre-filter, if applicable",
    )
