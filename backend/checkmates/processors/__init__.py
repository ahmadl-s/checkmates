"""Processor package: interfaces and implementations for downstream message processing."""

from checkmates.processors.base import MessageProcessor
from checkmates.processors.logging import LoggingProcessor

__all__ = ["MessageProcessor", "LoggingProcessor"]
