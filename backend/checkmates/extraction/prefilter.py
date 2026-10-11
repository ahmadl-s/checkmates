"""Deterministic pre-filter for selective AI extraction.

This component runs BEFORE the Gemini API call. It uses deterministic rules
(no AI calls) to skip messages that are clearly not actionable, saving
API quota for messages that plausibly contain tasks, commitments, or
deadlines.

Design principles:
    - **Conservative:** When uncertain, extract. Never silently discard a
      message that might contain an actionable task.
    - **Two-tier logic:** Negative signals (greetings, acknowledgments, pure
      questions) suggest skipping. Positive signals (commitment verbs, task
      nouns, deadline language) suggest extracting. Any positive signal
      overrides all negative signals.
    - **Not keyword-only:** Uses phrase patterns and structural heuristics
      (message length, question structure) rather than a tiny keyword list.
    - **Testable:** Pure function with no side effects or AI calls.
    - **Privacy:** Skip reasons are logged without full message content.
"""

from __future__ import annotations

import re

from checkmates.models import NormalizedMessage

# Minimum message length to consider (configurable in ExtractionService)
_DEFAULT_MIN_LENGTH = 10

# ---------------------------------------------------------------------------
# Negative signal patterns
# ---------------------------------------------------------------------------

# Greetings — typically have no actionable content
_GREETING_PATTERNS = [
    r"^(hi|hey|hello|yo|sup|howdy|greetings|good\s+(morning|afternoon|evening|night))\s*[!?.]*$",
    r"^(what'?s\s+up|how\s+(are|r)\s+(you|u|ya))\s*[!?.]*$",
    r"^(morning|afternoon|evening)\s*[!?.]*$",
]

# Acknowledgments — agreement without new commitments
_ACKNOWLEDGMENT_PATTERNS = [
    r"^(ok|okay|k|sure|yeah|yep|yup|nope|got\s+it|sounds\s+good|will\s+do|roger|acknowledged|agreed|makes\s+sense|i\s+see|noted|understood)\s*[!?.]*$",
    r"^(lol|haha|lmao|rofl|nice|cool|sweet|great|awesome|perfect|neat|wow|oh\s+(nice|cool|wow))\s*[!?.]*$",
    r"^(thanks|thank\s+you|thx|ty|cheers)\s*[!?.]*$",
    r"^(\+1|👍|✅|❤️|🔥|💯)\s*$",
]

# Question words — pure questions that are unlikely to contain commitments
_QUESTION_STARTERS = (
    "what", "why", "how", "when", "where", "who", "whose",
    "can", "could", "would", "should", "do", "does", "did", "is", "are",
    "am", "will", "shall", "may", "might", "have", "has", "had",
)

# ---------------------------------------------------------------------------
# Positive signal patterns
# ---------------------------------------------------------------------------

# Commitment language — someone is committing to do something
_COMMITMENT_PATTERNS = [
    r"\bi'?ll\b",
    r"\bi\s+will\b",
    r"\bi'?m\s+going\s+to\b",
    r"\bi\s+need\s+to\b",
    r"\bi\s+have\s+to\b",
    r"\bi\s+must\b",
    r"\bi\s+plan\s+to\b",
    r"\bi\s+can\b",
    r"\blet\s+me\b",
    r"\bi'?ll\s+take\b",
    r"\bi'?ll\s+handle\b",
    r"\bi'?ll\s+do\b",
    r"\bwe\s+should\b",
    r"\bwe\s+need\s+to\b",
    r"\bwe\s+have\s+to\b",
    r"\bwe'?ll\b",
    r"\bwe\s+will\b",
    r"\bsomeone\s+should\b",
    r"\bsomeone\s+needs?\s+to\b",
    r"\bwho\s+(is|can|will)\s+(do|handle|take|work\s+on)\b",
]

# Task-related verbs — action-oriented language
_TASK_VERB_PATTERNS = [
    r"\b(build|create|write|implement|design|develop|set\s+up|setup)\b",
    r"\b(review|test|deploy|fix|debug|refactor|document|submit|upload)\b",
    r"\b(finish|complete|start|begin|update|update|integrate|configure)\b",
    r"\b(organize|plan|schedule|prepare|draft|outline|research|analyze)\b",
    r"\b(code|program|script|install|run|execute|compile|package)\b",
    r"\b(present|demo|presented|presenting)\b",
    r"\b(assign|delegate|distribute|divide|split\s+up)\b",
    r"\b(meet|discuss|sync|standup|call)\b",
]

# Deadline language — temporal commitments
_DEADLINE_PATTERNS = [
    r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\bby\s+(today|tonight|tomorrow|end\s+of\s+(day|week|month))\b",
    r"\bby\s+\d{1,2}(/|-)\d{1,2}\b",
    r"\bby\s+(next|this)\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\bdeadline\b",
    r"\bdue\s+(by|on|today|tomorrow|this\s+week|next\s+week)\b",
    r"\b(tonight|tomorrow)\b",
    r"\bthis\s+week\b",
    r"\bnext\s+week\b",
    r"\bend\s+of\s+(day|week|month|semester)\b",
    r"\bbefore\s+(the\s+)?(meeting|class|deadline|presentation|demo)\b",
    r"\b(in|within)\s+\d+\s+(hour|day|week|minute)s?\b",
]

# Assignment language — task delegation
_ASSIGNMENT_PATTERNS = [
    r"\bassign(ed)?\b",
    r"\bresponsible\s+for\b",
    r"\bin\s+charge\s+of\b",
    r"\bi'?ll\s+take\b",
    r"\bi'?ll\s+handle\b",
    r"\b(you|he|she|they)\s+(should|need\s+to|have\s+to|will|can)\b",
    r"\bwho\s+(is|will|can)\s+(doing|handling|taking|working\s+on)\b",
    r"\blet'?s\s+have\s+\w+\s+do\b",
    r"\bowner\b",
]

# Dependency language — task ordering
_DEPENDENCY_PATTERNS = [
    r"\bdepends?\s+on\b",
    r"\bwaiting\s+for\b",
    r"\bblocked\s+by\b",
    r"\bafter\s+(you|we|i|they|he|she)\b",
    r"\bonce\s+(you|we|that|this)\b",
    r"\bprerequisite\b",
    r"\bbefore\s+(i|we|you|they|he|she)\s+(can|start|begin)\b",
]


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Compile a list of regex patterns, case-insensitive."""
    return [re.compile(p, re.IGNORECASE) for p in patterns]


class ExtractionPrefilter:
    """Deterministic pre-filter for selective AI extraction.

    Skips messages that are clearly not actionable without calling any
    AI model. When uncertain, defaults to extracting — never silently
    discards likely actionable messages.

    Usage:
        prefilter = ExtractionPrefilter(min_message_length=10)
        should_extract, skip_reason = prefilter.should_extract(message)
        if not should_extract:
            logger.info("Skipping extraction: %s", skip_reason)
            return
    """

    def __init__(self, *, min_message_length: int = _DEFAULT_MIN_LENGTH) -> None:
        self._min_length = max(1, min_message_length)
        self._greetings = _compile_patterns(_GREETING_PATTERNS)
        self._acknowledgments = _compile_patterns(_ACKNOWLEDGMENT_PATTERNS)
        self._commitments = _compile_patterns(_COMMITMENT_PATTERNS)
        self._task_verbs = _compile_patterns(_TASK_VERB_PATTERNS)
        self._deadlines = _compile_patterns(_DEADLINE_PATTERNS)
        self._assignments = _compile_patterns(_ASSIGNMENT_PATTERNS)
        self._dependencies = _compile_patterns(_DEPENDENCY_PATTERNS)

    @property
    def min_message_length(self) -> int:
        """The minimum message length to consider for extraction."""
        return self._min_length

    def should_extract(
        self, message: NormalizedMessage
    ) -> tuple[bool, str | None]:
        """Determine whether a message should be sent to AI extraction.

        Returns:
            A tuple of (should_extract, skip_reason). When should_extract
            is True, skip_reason is None. When False, skip_reason explains
            why the message was skipped.

        Decision logic:
            1. Check positive signals first. If any positive signal is
               found, extract immediately (override all negatives).
            2. If no positive signal, check negative signals. If any
               negative signal is found, skip.
            3. If no signal either way, extract (conservative default).
        """
        content = message.content.strip()

        # Step 1: Check positive signals first (overrides negatives)
        positive_match = self._check_positive_signals(content)
        if positive_match:
            return True, None

        # Step 2: Check negative signals
        negative_match = self._check_negative_signals(content)
        if negative_match:
            return False, negative_match

        # Step 3: Uncertain — extract (conservative)
        return True, None

    def _check_positive_signals(self, content: str) -> bool:
        """Check if the content contains any positive (actionable) signal."""
        for pattern_list in (
            self._commitments,
            self._task_verbs,
            self._deadlines,
            self._assignments,
            self._dependencies,
        ):
            for pattern in pattern_list:
                if pattern.search(content):
                    return True
        return False

    def _check_negative_signals(self, content: str) -> str | None:
        """Check if the content matches any negative (non-actionable) pattern.

        Returns the skip reason string if a negative signal is found,
        or None if no negative signal matches.
        """
        # Check greetings first (semantic identification before length)
        for pattern in self._greetings:
            if pattern.search(content):
                return "greeting"

        # Check acknowledgments
        for pattern in self._acknowledgments:
            if pattern.search(content):
                return "acknowledgment"

        # Check pure questions (short, ends with ?, starts with question word)
        if content.endswith("?"):
            first_word = content.split()[0].lower().rstrip("?,.!") if content.split() else ""
            if first_word in _QUESTION_STARTERS and len(content) < 120:
                return "pure_question"

        # Check emoji-only or reaction-only messages
        stripped = content.strip()
        if stripped and not any(c.isalnum() for c in stripped):
            return "emoji_only"

        # Check very short messages (fallback for unrecognized short content)
        if len(content) < self._min_length:
            return "too_short"

        return None
