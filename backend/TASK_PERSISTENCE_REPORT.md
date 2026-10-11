# Task Persistence Report

## Summary

Implemented the smallest reliable feature that converts successful Gemini task extractions into persistent task records in the existing `tasks` SQLite table. The implementation reuses the existing architecture — no parallel task model, no new tables, no external services.

**Full regression suite: 224 passed, 0 failed** (211 prior + 13 new)

---

## Changed Files

| # | File | Change |
|---|------|--------|
| 1 | `checkmates/database/schema.py` | Added partial unique index `idx_tasks_source_title` on `tasks(source_message_id, title) WHERE source_message_id IS NOT NULL` |
| 2 | `checkmates/database/models.py` | Changed `Task.deadline` field type from `datetime \| None` to `str \| None` (DB column is TEXT; supports both ISO datetimes and raw AI text) |
| 3 | `checkmates/database/repository.py` | Added `save_extracted_task()` method — INSERT OR IGNORE with dedup via unique index |
| 4 | `checkmates/processors/extraction.py` | Added optional `repository` param + `_persist_tasks()` method in worker |
| 5 | `checkmates/__main__.py` | Pass `repository=repo` to ExtractionProcessor |
| 6 | `tests/test_extraction_pipeline.py` | Updated `processor` fixture (adds repository), renamed `TestNoAutoPersist` → `TestTaskPersistence` |
| 7 | `tests/test_task_persistence.py` | **New file** — 13 focused tests |

---

## How Extracted Tasks Reach SQLite

```
Discord message
  → MessageCollector.on_message()
  → MessageFilter.should_process()          # channel whitelist
  → DeduplicationCache.is_seen_original()   # in-memory dedup
  → MessageNormalizer.normalize()           # → NormalizedMessage
  → ExtractionProcessor.process()
      1. PersistenceProcessor.process()     # → repo.upsert_message() → DB
      2. queue.put_nowait(message)           # enqueue for extraction
  → ExtractionProcessor._worker()           # background single-worker
      → ExtractionService.extract(message)  # → Gemini API → ExtractionResult
      → if result.error_type is None and result.has_actionable_tasks:
          → _persist_tasks(message, result)
              1. repo.get_message_by_discord_id()  # resolve Discord ID → internal messages.id
              2. for each ExtractedTask in result.tasks:
                  → repo.save_extracted_task(
                      project_id=result.context_project_id,
                      title=task.title,
                      description=task.description,
                      assignee_id=task.assignee_user_id,    # resolved by ExtractionService
                      deadline_text=task.deadline_text,      # raw text, e.g. "by Friday"
                      source_message_id=stored.id,           # internal messages.id
                    )
              # INSERT OR IGNORE → dedup via unique index on (source_message_id, title)
              # Returns Task if new, None if duplicate
```

### Field Mapping

| ExtractedTask field | tasks table column | Notes |
|---------------------|--------------------|----|
| `title` | `title` | Required, NOT NULL |
| `description` | `description` | Optional, NULL if not provided |
| `assignee_user_id` | `assignee_id` | Resolved Discord user ID from project members. NULL if no assignee or name not matched. |
| `deadline_text` | `deadline` | Raw AI text (e.g., "by Friday"). Stored directly as TEXT. NULL if not identified. |
| `source_message_id` (Discord) | `source_message_id` (internal) | Resolved via `get_message_by_discord_id()` to internal `messages.id` for FK integrity |
| (N/A) | `project_id` | From `result.context_project_id` |
| (N/A) | `status` | Always `'pending'` |

---

## Deduplication Guarantee

Three layers of dedup prevent duplicate task records:

1. **Extraction dedup cache** (in-memory, `ExtractionDedupCache`) — prevents re-calling Gemini for unchanged messages. Resets on restart.

2. **Database unique index** (authoritative) — `idx_tasks_source_title` on `tasks(source_message_id, title) WHERE source_message_id IS NOT NULL`. Enforced at the SQLite level. `INSERT OR IGNORE` silently skips duplicates.

3. **Message persistence dedup** — `UNIQUE(discord_message_id)` on the `messages` table prevents duplicate source messages.

The partial unique index only applies when `source_message_id IS NOT NULL`, so manually created tasks (which don't have a source message) are unaffected.

---

## Optional Field Handling

- **No assignee**: `assignee_user_id` is None → `assignee_id` column = NULL
- **Unresolved assignee** (AI names someone not in project members): `assignee_user_id` is None (confidence="medium") → `assignee_id` column = NULL
- **No deadline**: `deadline_text` is None → `deadline` column = NULL
- **No description**: `description` is None → `description` column = NULL

---

## Error Handling

- `ExtractionService.extract()` never raises — returns `ExtractionResult` with `error_type` for failures. Tasks are only persisted when `error_type is None`.
- `_persist_tasks()` wraps all DB operations in try/except. If `save_extracted_task()` raises, the exception is logged and the worker continues processing the next message.
- If the source message is not found in the DB (should not happen — it was just persisted), tasks are skipped with a warning log.
- Persistence failures do not crash the Discord bot.

---

## Test Results

**224 passed, 0 failed** in 11.74s

### New tests in `test_task_persistence.py` (13 tests)

| Class | Test | What it covers |
|-------|------|----------------|
| `TestSuccessfulPersistence` | `test_tasks_saved_to_db` | 2 tasks persisted, correct count in DB |
| | `test_source_message_traceability` | `source_message_id` points to internal `messages.id` |
| | `test_assignee_resolved_to_discord_id` | Assignee name "Alice" → `assignee_id=1001` |
| | `test_no_tasks_when_extraction_returns_none` | No tasks when AI finds nothing |
| `TestDuplicatePrevention` | `test_same_message_same_title_no_duplicate` | `save_extracted_task` called twice → only 1 row |
| | `test_different_titles_same_message_both_saved` | Same message, different titles → 2 rows |
| | `test_reprocessed_message_no_new_tasks` | Full pipeline: process same message twice → 1 task |
| `TestOptionalFields` | `test_no_assignee` | No assignee → `assignee_id is None` |
| | `test_unresolved_assignee` | Name not in members → `assignee_id is None` |
| | `test_deadline_text_stored_as_text` | "by end of next week" stored directly |
| | `test_no_description` | No description → `description is None` |
| `TestDatabaseFailure` | `test_persistence_failure_does_not_crash_worker` | `save_extracted_task` raises → worker survives |
| | `test_worker_continues_after_persistence_error` | First fails, second succeeds → worker running, 2nd task saved |

### Updated test in `test_extraction_pipeline.py`

- `TestNoAutoPersist.test_extracted_tasks_not_saved_to_db` → renamed to `TestTaskPersistence.test_extracted_tasks_saved_to_db` — now asserts tasks ARE saved with correct field mapping

---

## Live Demo Gaps

1. **Project registration required**: The `projects` and `project_channels` tables must be populated before extraction works. Run:
   ```python
   project = await repo.create_project(server_id=<guild_id>, name="Demo")
   await repo.add_project_channel(project.id, <channel_id>, <guild_id>)
   ```

2. **GEMINI_API_KEY must be set**: Without it, `ExtractionService` returns `config_error` and no tasks are extracted. The `.env` file currently has it set.

3. **Discord bot must be running**: Send a message in the registered channel that passes the pre-filter (actionable content, >10 chars). The bot collects, persists, extracts, and now persists tasks.

4. **Verify tasks in SQLite**:
   ```bash
   cd backend
   .venv/Scripts/python.exe -c "
   import sqlite3; conn = sqlite3.connect('checkmates.db')
   for r in conn.execute('SELECT id, title, assignee_id, deadline, status, source_message_id FROM tasks'):
       print(r)
   "
   ```

5. **Extracted tasks are proposals**: Tasks are saved with `status='pending'`. There is no UI, dashboard, or Discord notification to display them. Verification is via direct DB inspection.

---

## What Was NOT Changed

- No new database tables (uses existing `tasks` table)
- No new AI providers (Gemini-only)
- No Discord notifications or external actions
- No schema migrations (only added a new index via `CREATE INDEX IF NOT EXISTS`)
- No changes to message persistence or extraction queue behavior
- `ExtractionProcessor.repository` defaults to `None` — callers that don't pass it get the old behavior (no task persistence), fully backward compatible
