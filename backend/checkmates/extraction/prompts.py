"""System and user prompt builders for AI task extraction.

The system prompt provides project context (members, recent messages,
existing tasks) so the AI can identify actionable tasks and resolve
assignee names. The user prompt provides the message to analyze.

Design principles:
    - The AI never sees Discord IDs or internal database IDs.
    - Members are presented by display name so the AI can match
      assignee mentions to known project members.
    - The AI is instructed NOT to invent assignees, deadlines, or
      commitments that are not explicitly stated in the message.
    - Uncertainty is expected and should be noted, not guessed.
"""

from __future__ import annotations

from checkmates.context.models import ProjectContext
from checkmates.models import NormalizedMessage


def build_system_prompt(context: ProjectContext) -> str:
    """Build the system prompt with project context.

    The prompt instructs the AI to:
    - Identify actionable tasks (not opinions, questions, or chatter)
    - Extract title, description, assignee, deadline only when explicitly stated
    - Identify task completions — when someone reports finishing an existing task
    - Match completions against the Existing Tasks list
    - Note uncertainty in uncertainty_notes rather than guessing
    - Return an empty tasks list if no actionable tasks are found
    """
    parts: list[str] = [
        "You are a task extraction assistant for a university group project management bot "
        "called Checkmates. Your job is to read Discord messages and identify two things:\n"
        "1. NEW actionable tasks that project members have committed to or been assigned.\n"
        "2. COMPLETIONS — when someone reports that an existing task has been finished.",
        "",
        "## Rules for New Tasks (tasks list)",
        "",
        "1. Identify only ACTIONABLE tasks — things someone needs to DO. "
        "Ignore questions, opinions, status updates without commitments, and casual chatter.",
        "2. Extract the task title in imperative mood (e.g., 'Build API endpoint', not 'Bob will do API').",
        "3. Extract a description with additional context if available.",
        "4. Only include an assignee_name if the message EXPLICITLY mentions who is doing the task. "
        "Do NOT guess or invent assignees.",
        "5. Only include a deadline_text if the message EXPLICITLY states a deadline. "
        "Do NOT guess or invent deadlines.",
        "6. If information is missing or ambiguous, note it in uncertainty_notes. "
        "Do not fill in missing data with assumptions.",
        "7. If the message contains no new actionable tasks, return an empty tasks list "
        "with has_actionable_tasks=false.",
        "8. Set confidence (0.0 to 1.0) for each task based on how clearly it is stated.",
        "",
        "## Rules for Task Completions (completed_tasks list)",
        "",
        "1. Identify when a message clearly states that an EXISTING task has been completed. "
        "Examples: 'I'm done with the login', 'I've finished the API tests', "
        "'The login task is complete'.",
        "2. Match the completion to a task from the Existing Tasks list below. "
        "Use the exact task title when possible.",
        "3. Put the matched task title in the task_title field.",
        "4. Do NOT create a new task for a completion statement. "
        "If the message is about finishing something, put it in completed_tasks, NOT in tasks.",
        "5. Only include completions when the message CLEARLY indicates completion. "
        "Phrases like 'I'm working on the login' are NOT completions — they are status updates.",
        "6. If it's unclear which task was completed, note it in uncertainty_notes.",
        "7. Set has_completions=true if at least one completion is identified.",
        "8. Set confidence (0.0 to 1.0) based on how clearly the completion is stated.",
        "",
    ]

    # Project context
    if context.project is not None:
        parts.append(f"## Project: {context.project.name}")
        parts.append("")

    # Members
    if context.members:
        parts.append("## Project Members")
        for member in context.members:
            name = member.display_name or f"User {member.discord_user_id}"
            parts.append(f"- {name}")
        parts.append("")
    else:
        parts.append("## Project Members")
        parts.append("(No members registered yet)")
        parts.append("")

    # Existing tasks (for context — so the AI can avoid duplicates and match completions)
    if context.tasks:
        parts.append("## Existing Tasks")
        for task in context.tasks:
            status_str = f" [{task.status}]" if task.status != "pending" else ""
            parts.append(f"- {task.title}{status_str}")
        parts.append("")
    else:
        parts.append("## Existing Tasks")
        parts.append("(No existing tasks yet)")
        parts.append("")

    # Recent messages (for context — so the AI understands conversation flow)
    if context.recent_messages:
        parts.append("## Recent Messages (chronological)")
        for msg in context.recent_messages:
            author = msg.author_display_name or "Unknown"
            ts = msg.message_timestamp.strftime("%Y-%m-%d %H:%M") if msg.message_timestamp else "?"
            parts.append(f"[{ts}] {author}: {msg.content}")
        parts.append("")

    return "\n".join(parts)


def build_user_prompt(message: NormalizedMessage) -> str:
    """Build the user prompt with the message to analyze.

    The prompt presents the message with its author and timestamp so
    the AI has full context for extraction.
    """
    author = message.author_display_name or "Unknown"
    ts = message.timestamp.strftime("%Y-%m-%d %H:%M") if message.timestamp else "Unknown time"

    parts: list[str] = [
        "## Message to Analyze",
        "",
        f"Author: {author}",
        f"Timestamp: {ts}",
        f"Content: {message.content}",
        "",
        "Extract any new actionable tasks and/or task completions from this message. "
        "If none are present, return empty lists for both tasks and completed_tasks.",
    ]

    return "\n".join(parts)
