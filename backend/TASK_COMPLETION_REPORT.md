# Task Completion Detection — Final Report

## Summary

Extended Checkmates' Gemini extraction pipeline to automatically detect when users report completing existing tasks. The AI now distinguishes between **new commitments** (create tasks) and **completion statements** (mark existing tasks completed), using a structured `completed_tasks` list in the extraction response schema.

Task creation behavior is fully preserved. Completion detection is additive and backward compatible.

---

## Changed Files

| File | Change |
|------|--------|
| `checkmates/extraction/models.py` | Added `AICompletedTask`, `CompletedTaskRef` models; extended `AITaskExtractionResponse` with `completed_tasks` + `has_completions`; extended `ExtractionResult` with `completed_tasks` + `has_completions` |
| `checkmates/extraction/__init__.py` | Exported `AICompletedTask` and `CompletedTaskRef` |
| `checkmates/extraction/prompts.py` | Added "Rules for Task Completions" section to system prompt; updated user prompt to mention completions; added "No existing tasks yet" fallback |
| `checkmates/extraction/service.py` | Added `_enrich_completion()` method; updated `extract()` to enrich completions and include them in `ExtractionResult` |
| `checkmates/processors/extraction.py` | Added `_process_completions()` method with title matching; updated worker to call it when `result.has_completions` is True |
| `tests/test_task_completion.py` | **New file** — 15 tests across 12 test classes |

No dependencies were added. No existing tests were modified.

---

## New Behavior

### Task Creation (unchanged)
- "I'll do the login" → creates a pending task assigned to the message author.
- "I'll handle the API tests" → creates a pending task.
- Existing extraction, enrichment, and persistence behavior is fully preserved.

### Task Completion (new)
- "I'm done with the login" → finds the existing "login" task in the current project and marks it `completed`.
- "I've finished the API tests" → marks the matching task `completed`.
- "The login task is complete" → marks the matching task `completed`.
- Completion messages do **not** create duplicate pending tasks.
- Already-completed tasks are not re-processed.
- Completed tasks are not deleted — they remain visible in task listings.

### Structured AI Output
The Gemini response schema (`AITaskExtractionResponse`) now includes:
- `tasks: list[AIExtractedTask]` — new tasks to create (existing field)
- `completed_tasks: list[AICompletedTask]` — existing tasks to mark completed (new field)
- `has_actionable_tasks: bool` — whether new tasks were found (existing field)
- `has_completions: bool` — whether completions were found (new field, defaults to `False`)

Both lists default to empty, so existing tests that construct `AITaskExtractionResponse(tasks=[...])` without completion fields continue to work.

---

## How Matching Works

The `_process_completions()` method in `ExtractionProcessor` performs title-based matching:

1. **Retrieve project tasks** — calls `repo.get_tasks_by_project(project_id)` for the project associated with the message's channel.
2. **Filter to pending only** — already-completed tasks are skipped (no re-completion).
3. **Exact match (case-insensitive)** — compares the AI-provided `task_title` against each pending task's title.
4. **Substring match (fallback)** — if no exact match, checks whether the completion title is a substring of the task title or vice versa (e.g., "login" matches "Implement login page").
5. **Single match → complete** — if exactly one task matches, calls `repo.update_task_status(task.id, "completed")`.
6. **No match → no change** — logs a diagnostic (task title only, no message content).
7. **Multiple matches → no change** — logs a diagnostic about ambiguity; no task is modified.

All exceptions in `_process_completions()` are caught and logged. Completion processing failures never crash the worker or prevent message persistence.

---

## How Project Isolation Works

- Completion matching queries tasks via `get_tasks_by_project(project_id)`, where `project_id` is resolved from the message's Discord channel through the `project_channels` table.
- A completion message in Project A's channel can only match tasks belonging to Project A.
- A completion message in Project B's channel can only match tasks belonging to Project B.
- There is no cross-project task lookup at any point in the completion pipeline.

This is verified by `TestProjectIsolation::test_completion_in_project_a_does_not_affect_project_b`.

---

## Test Results

```
253 passed in 15.02s
```

### New tests (15 in `tests/test_task_completion.py`)

| Class | Test | Scenario |
|-------|------|----------|
| `TestTaskCreationRegression` | `test_commitment_creates_pending_task` | "I'll do the login" creates a pending task |
| `TestAuthorAssignment` | `test_task_assigned_to_message_author` | New task assigned to message author via member mapping |
| `TestTaskCompletion` | `test_completion_marks_task_completed` | "I'm done with the login" marks matching task completed |
| `TestTaskCompletion` | `test_completion_by_substring_match` | "login" substring matches "Implement login page" |
| `TestNoDuplicateOnCompletion` | `test_completion_does_not_create_new_task` | Completion message creates zero new tasks |
| `TestNoMatch` | `test_no_matching_task_no_change` | Completion with no matching task → no status changes |
| `TestAmbiguousMatch` | `test_ambiguous_match_no_change` | Multiple matching tasks → no status changes |
| `TestProjectIsolation` | `test_completion_in_project_a_does_not_affect_project_b` | Completion in Project A doesn't touch Project B tasks |
| `TestRepeatedCompletion` | `test_repeated_completion_idempotent` | Second completion message for same task is a no-op |
| `TestOrdinaryMessages` | `test_ordinary_discussion_no_changes` | Casual discussion creates/changes no tasks |
| `TestApiErrors` | `test_api_error_message_persisted_no_task_changes` | Gemini API error → message persisted, no task changes |
| `TestMalformedOutput` | `test_malformed_response_no_crash` | Malformed AI response handled gracefully |
| `TestRegression` | `test_existing_task_persistence_still_works` | Task creation + persistence still works alongside completion |
| `TestRegression` | `test_completion_does_not_delete_task` | Completed task remains in DB (not deleted) |
| `TestRegression` | `test_already_completed_task_not_recompleted` | Already-completed task is not re-processed |

### Existing tests (238)
All 238 previously passing tests continue to pass unchanged, confirming backward compatibility.

---

## Limitations

1. **Title-based matching only** — matching uses task titles (exact, then substring). It does not use task descriptions or semantic similarity. A completion like "I finished the auth stuff" may not match a task titled "Implement login page" even if they refer to the same work.

2. **No assignee verification on completion** — the completion matcher does not check whether the person reporting completion is the task's assignee. Anyone in the channel can mark a task completed. This matches the user's requirement: "do not assume the speaker must be the task's assignee."

3. **Ambiguous matches result in no action** — if multiple pending tasks match a completion title, no task is modified. This is conservative by design but means some legitimate completions may be missed.

4. **AI confidence is captured but not thresholded** — the `confidence` field from the AI response is stored in `CompletedTaskRef` but is not used to gate the completion. All AI-identified completions are processed. A confidence threshold could be added if false positives become an issue.

5. **No notification on completion** — completing a task updates the DB status silently. There is no Discord reply or reaction confirming the completion to the user.

6. **Completion requires an existing task** — if a user says "I'm done with the login" but no "login" task exists in the project, no action is taken and no new task is created.
