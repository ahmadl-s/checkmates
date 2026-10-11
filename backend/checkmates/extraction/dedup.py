"""Extraction deduplication cache.

Tracks which messages have already been sent to the AI extraction
service so that unchanged messages are not re-extracted unnecessarily.
Edited messages (with different content) produce a different hash and
will be re-extracted.

Design:
    - Key: (message_id, content_hash) where content_hash is sha256 of
      the message content. An unchanged message produces the same hash;
      an edited message produces a different hash → re-extract.
    - Bounded FIFO cache (OrderedDict) to cap memory usage.
    - Only successful extractions are marked — a failed API request is
      NOT marked, so it can be retried on the next event.
    - Does NOT persist across restarts. The provider's own rate limits
      and the application-level daily cap provide restart-safe guards.

This is separate from the collection-level DeduplicationCache, which
tracks Discord event deduplication (whether we've seen this event before).
The extraction dedup tracks whether we've already sent this specific
content to the AI — a message can be collected once but extracted once,
and an edited message can be collected again and extracted again.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

# Maximum entries before oldest are evicted.
_DEFAULT_MAX_SIZE = 10_000


class ExtractionDedupCache:
    """Bounded in-memory cache for extraction deduplication.

    Tracks (message_id, content_hash) pairs of successfully extracted
    messages. Prevents redundant API calls for unchanged content.
    """

    def __init__(self, maxsize: int = _DEFAULT_MAX_SIZE) -> None:
        self._maxsize = maxsize
        self._seen: OrderedDict[tuple[int, str], None] = OrderedDict()

    @staticmethod
    def _hash_content(content: str) -> str:
        """Compute a SHA-256 hash of the message content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def is_extracted(self, message_id: int, content: str) -> bool:
        """Check if this message (with this exact content) has been extracted.

        Returns True if the message was previously extracted with the same
        content. Returns False if the message was never extracted or if the
        content has changed since the last extraction.

        This is a read-only check — it does NOT mark the message.
        """
        key = (message_id, self._hash_content(content))
        return key in self._seen

    def mark_extracted(self, message_id: int, content: str) -> None:
        """Mark a message as successfully extracted.

        Only call this AFTER a successful extraction. Failed API requests
        must NOT be marked — otherwise they cannot be retried.
        """
        key = (message_id, self._hash_content(content))
        if key not in self._seen:
            self._seen[key] = None
            if len(self._seen) > self._maxsize:
                self._seen.popitem(last=False)

    def reset(self) -> None:
        """Clear all dedup state. Useful for testing."""
        self._seen.clear()

    def __len__(self) -> int:
        """Number of cached entries."""
        return len(self._seen)
