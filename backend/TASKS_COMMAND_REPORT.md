# !tasks Command — Implementation Report

## Summary

Added a `!tasks` Discord command that lists saved tasks for the project mapped to the current channel. The command reads from SQLite only — no Gemini calls, no AI service. All 238 tests pass (14 new tests added).

## Changed Files

| File | Change |
|------|--------|
| `checkmates/collector.py` | Added `repository` param, `!tasks` interception in `on_message`, `_handle_tasks_command()` method, `_split_message()` helper |
| `checkmates/__main__.py` | Pass `repository=repo` to `MessageCollector` constructor |
| `tests/test_tasks_command.py` | **New file** — 14 tests across 8 test classes |

## Command Syntax

```
!tasks
```

Case-insensitive, whitespace-trimmed. Examples that trigger the command:
- `!tasks`
- `  !tasks  `
- `!Tasks`
- `!TASKS`

The command only works when a `repository` is provided to `MessageCollector` (which `__main__.py` now does). Without a repository, `!tasks` falls through to the normal collection pipeline (backward compatible).

## How It Works

### Command Interception

In `MessageCollector.on_message()`, before the normal filter → dedup → normalize → process pipeline:

1. Check if `repository` is provided AND message author is not a bot
2. Check if `message.content.strip().lower() == "!tasks"`
3. If yes → call `_handle_tasks_command(message)` and return (never reaches the processor)
4. If no → continue to the normal collection pipeline

### _handle_tasks_command Flow

```
!tasks message
    ↓
repo.get_project_by_channel(channel_id)
    ↓
    ├── project is None → "No project is mapped to this channel..."
    ├── project found, no tasks → "No tasks have been recorded for {project} yet..."
    └── project found, tasks exist → format + split + send
```

### Task Formatting

Each task is displayed as:
```
1. **Build API** `[pending]` — Assignee: Alice | Deadline: 2025-01-15T10:00:00+00:00
```

- **Title** in bold
- **Status** in inline code
- **Assignee** resolved from `members` table via `discord_user_id` lookup; "Unassigned" if `assignee_id` is None
- **Deadline** shown as stored (ISO datetime or raw text); "Not set" if None

### Message Splitting

`_split_message()` splits output on newline boundaries to respect Discord's 2000-character message limit. If a single line exceeds the limit, it splits at word boundaries. Long task lists produce multiple `channel.send()` calls.

## How Project Isolation Is Enforced

Project isolation is enforced at the **database query level**, not in application code:

1. `repo.get_project_by_channel(channel_id)` performs a JOIN on `project_channels` → `projects` filtered by `channel_id`. Only the project mapped to this specific channel is returned.

2. `repo.get_tasks_by_project(project_id)` queries `SELECT * FROM tasks WHERE project_id = ?`. Only tasks belonging to that project are returned.

3. There is no way for tasks from another project to leak — the `project_id` filter in the SQL query is the authoritative boundary.

4. Even if two projects share the same Discord server, their channels are distinct (each channel maps to at most one project via the `UNIQUE` constraint on `project_channels.channel_id`).

Tests verify this explicitly:
- `test_channel_a_shows_only_project_a_tasks` — channel 2001 (Alpha) shows only Alpha's tasks, not Beta's
- `test_channel_b_shows_only_project_b_tasks` — channel 2002 (Beta) shows only Beta's tasks, not Alpha's

## Test Results

```
238 passed in 13.83s
```

### New Tests (14)

| Class | Test | What it verifies |
|-------|------|------------------|
| `TestProjectIsolation` | `test_channel_a_shows_only_project_a_tasks` | Channel A shows only Alpha's tasks |
| `TestProjectIsolation` | `test_channel_b_shows_only_project_b_tasks` | Channel B shows only Beta's tasks |
| `TestEmptyResults` | `test_no_tasks_friendly_message` | Project with no tasks → friendly message |
| `TestNoProjectMapped` | `test_unmapped_channel_friendly_message` | Unmapped channel → "no project" message |
| `TestTaskFormatting` | `test_title_status_assignee_deadline_shown` | Title, status, assignee, deadline all present |
| `TestTaskFormatting` | `test_unassigned_task_shows_unassigned` | Missing assignee → "Unassigned" |
| `TestCommandNotProcessed` | `test_tasks_command_not_forwarded` | !tasks never reaches the processor |
| `TestMessageSplitting` | `test_many_tasks_split_into_multiple_messages` | 30 tasks → multiple Discord messages |
| `TestMessageSplitting` | `test_split_message_short_text` | Short text → single chunk |
| `TestMessageSplitting` | `test_split_message_exact_limit` | Exact 2000 chars → single chunk |
| `TestMessageSplitting` | `test_split_message_over_limit` | 30 lines × 100 chars → multiple chunks |
| `TestMessageSplitting` | `test_split_message_single_long_line` | Single long line → word-boundary split |
| `TestBackwardCompat` | `test_no_repository_falls_through_to_processor` | Without repository, !tasks is a normal message |
| `TestBotMessagesIgnored` | `test_bot_author_not_intercepted` | Bot's own !tasks is not handled |

## Requirements Compliance

| # | Requirement | Status |
|---|-------------|--------|
| 1 | `!tasks` lists tasks for the project mapped to the current channel | Done |
| 2 | Shows title, status, assignee, deadline | Done |
| 3 | Empty results → friendly message | Done |
| 4 | Never shows tasks from another project/server | Done (DB-level isolation) |
| 5 | Splits long output to respect Discord's 2000-char limit | Done |
| 6 | No Gemini calls — SQLite only | Done |
| 7 | Existing message collection and extraction behavior intact | Done (238 tests pass) |
| 8 | Focused tests for project isolation, empty results, formatting | Done (14 new tests) |
| 9 | No task creation commands, reminders, dashboards, new deps, major refactors | Done |
| 10 | No commit or push | Done |

## Setup Required for Live Demo

1. **Project + channel mapping must exist in the database.** The `project_channels` table must have an entry mapping the Discord channel to a project. This is NOT auto-created by the bot — it must be set up manually:

   ```python
   # Using the venv Python
   from checkmates.database import Database, MessageRepository
   db = Database("backend/checkmates.db")
   await db.init()
   repo = MessageRepository(db)
   project = await repo.create_project(server_id=<guild_id>, name="My Project")
   await repo.add_project_channel(project.id, <channel_id>, <guild_id>)
   await db.close()
   ```

2. **Tasks must exist in the database.** Either:
   - Send task-related messages so Gemini extraction creates them (requires `GEMINI_API_KEY` set and project context configured), OR
   - Create them manually via `repo.create_task(project.id, "Task title", assignee_id=<user_id>)`

3. **Bot must be running** with `python -m checkmates` and connected to Discord with `message_content` and `members` intents enabled.

4. **Type `!tasks`** in any channel that has a project mapping. The bot will reply with the task list or a friendly message if no tasks exist.
