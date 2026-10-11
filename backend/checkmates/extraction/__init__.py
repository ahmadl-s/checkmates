"""Extraction package: AI task extraction from Discord messages.

Exports:
    ExtractionService  — extracts candidate tasks from messages using Gemini
    ExtractionResult   — the full result of an extraction operation
    ExtractedTask      — a single enriched candidate task
    CompletedTaskRef   — a reference to a task to mark completed
    AITaskExtractionResponse — the Pydantic model used as Gemini response_schema
    AIExtractedTask    — a single AI-identified task (before enrichment)
    AICompletedTask    — a single AI-identified completion (before enrichment)
    ExtractionPrefilter — deterministic pre-filter for selective extraction
    ExtractionDedupCache — bounded cache for extraction deduplication
"""

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
from checkmates.extraction.service import ExtractionService

__all__ = [
    "ExtractionService",
    "ExtractionResult",
    "ExtractedTask",
    "CompletedTaskRef",
    "AITaskExtractionResponse",
    "AIExtractedTask",
    "AICompletedTask",
    "ExtractionPrefilter",
    "ExtractionDedupCache",
]
