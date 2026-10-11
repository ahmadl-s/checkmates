"""Context package: project context retrieval for AI and agentic workflows.

Exports:
    ContextService  — retrieves structured project context from the database
    ProjectContext   — the top-level context model returned by the service
    ContextMetadata  — completeness flags within ProjectContext
"""

from checkmates.context.models import ContextMetadata, ProjectContext
from checkmates.context.service import ContextService

__all__ = ["ContextService", "ProjectContext", "ContextMetadata"]
