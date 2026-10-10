"""In-process deduplication cache for message collection.

This prevents processing the same message or edit multiple times within a
single running process. It does NOT persist across restarts — that will be
handled by the database layer (Feature 2) in a later implementation task.

Design:
    - Original messages: deduplicated by message_id.
    - Edited messages: deduplicated by (message_id, edited_timestamp).
      This allows each distinct edit to be forwarded once.
    - Both caches are bounded FIFO sets (OrderedDict) to cap memory usage.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime

# Maximum entries per cache before oldest are evicted.
_DEFAULT_MAX_SIZE = 10_000


class DeduplicationCache:
    """Bounded in-process deduplication for original messages and edits."""

    def __init__(self, maxsize: int = _DEFAULT_MAX_SIZE) -> None:
        self._maxsize = maxsize
        self._seen_originals: OrderedDict[int, None] = OrderedDict()
        self._seen_edits: OrderedDict[tuple[int, float | None], None] = OrderedDict()

    def should_process_original(self, message_id: int) -> bool:
        """Return True if this original message hasn't been seen (and mark it seen)."""
        if message_id in self._seen_originals:
            return False
        self._seen_originals[message_id] = None
        if len(self._seen_originals) > self._maxsize:
            self._seen_originals.popitem(last=False)
        return True

    def should_process_edit(
        self, message_id: int, edited_timestamp: datetime | None
    ) -> bool:
        """Return True if this edit hasn't been seen (and mark it seen)."""
        key: tuple[int, float | None] = (
            message_id,
            edited_timestamp.timestamp() if edited_timestamp is not None else None,
        )
        if key in self._seen_edits:
            return False
        self._seen_edits[key] = None
        if len(self._seen_edits) > self._maxsize:
            self._seen_edits.popitem(last=False)
        return True

    def reset(self) -> None:
        """Clear all dedup state. Useful for testing."""
        self._seen_originals.clear()
        self._seen_edits.clear()
