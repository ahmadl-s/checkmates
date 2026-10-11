"""Processor package: interfaces and implementations for downstream message processing."""

from checkmates.processors.base import MessageProcessor
from checkmates.processors.extraction import ExtractionProcessor
from checkmates.processors.logging import LoggingProcessor
from checkmates.processors.persistence import PersistenceProcessor

__all__ = [
    "MessageProcessor",
    "LoggingProcessor",
    "PersistenceProcessor",
    "ExtractionProcessor",
]
