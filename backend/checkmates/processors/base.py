"""MessageProcessor: the interface between collection and downstream processing.

The collector produces NormalizedMessage objects and calls processor.process().
Future implementations (AI extraction, context retrieval, database writes)
will implement this interface. This keeps the collection layer decoupled
from the intelligence layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from checkmates.models import NormalizedMessage


@runtime_checkable
class MessageProcessor(Protocol):
    """Interface for processing normalized messages.

    Implementations should be async and must not raise — the collector
    wraps calls in try/except, but processors should handle their own
    errors gracefully for production resilience.
    """

    async def process(self, message: NormalizedMessage) -> None:
        """Process a single normalized message.

        Args:
            message: A NormalizedMessage produced by the collection layer.
        """
        ...
