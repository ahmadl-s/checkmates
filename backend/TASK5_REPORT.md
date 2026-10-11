# Task 5 — Final Report: Integrate Selective Gemini Extraction into the Discord Pipeline

## 1. Architecture

The pipeline is a three-layer chain:

```
Discord I/O          Processing Pipeline                    Data Layer
────────────         ──────────────────                     ──────────
MessageCollector →   ExtractionProcessor                   Database (SQLite)
  (discord.Client)      ├─ PersistenceProcessor (wrapped)   ├─ MessageRepository
  filter                │   └─ upsert_message()             ├─ project_channels
  dedup (in-mem)        ├─ asyncio.Queue (bounded, cap=50)  ├─ tasks
  normalize            └─ Worker (single background task)   └─ members
  → processor.process()       └─ ExtractionService.extract()
                                  ├─ ExtractionPrefilter (regex)
                                  ├─ ContextService.get_context()
                                  ├─ ExtractionDedupCache (sha256)
                                  ├─ asyncio.Lock (daily cap)
                                  ├─ asyncio.Semaphore (concurrency)
                                  └─ Gemini API (structured output)
```

**ExtractionProcessor** wraps **PersistenceProcessor**. Every message is
persisted first; only then is it enqueued for background AI extraction.
The collector calls `ExtractionProcessor.process()` — it never knows
extraction exists.

## 2. Files Changed/Created

| File | Action | Summary |
|------|--------|---------|
| `checkmates/processors/extraction.py` | **Created** | ExtractionProcessor: bounded queue, single worker, shutdown, saturation |
| `checkmates/extraction/service.py` | **Modified** | Added `asyncio.Lock` for race-safe daily cap check-and-increment |
| `checkmates/processors/__init__.py` | **Modified** | Added `ExtractionProcessor` to exports |
| `checkmates/config.py` | **Modified** | Added `extraction_queue_capacity` (50) and `extraction_shutdown_timeout` (10.0) |
| `checkmates/__main__.py` | **Modified** | Wired ExtractionProcessor with ContextService + ExtractionService; shutdown before DB close |
| `.env.example` | **Modified** | Documented `EXTRACTION_QUEUE_CAPACITY` and `EXTRACTION_SHUTDOWN_TIMEOUT` |
| `tests/test_extraction_pipeline.py` | **Created** | 20 integration tests covering all 10 required scenarios |

## 3. Ordering: Persist Before Extract

`ExtractionProcessor.process()` executes in strict sequence:

1. `await self._wrapped.process(message)` — **persist first**. If this raises (DB error), the exception propagates up to the collector, which does NOT mark the dedup cache, allowing retry on the next event. The message is never enqueued.
2. If `_stopped` is True (shutdown in progress), return. Message is already persisted.
3. `_ensure_worker()` — lazily start/restart the background worker.
4. `self._queue.put_nowait(message)` — non-blocking enqueue. If `QueueFull`, log warning and skip extraction. Message remains persisted.

This ordering guarantees: **no message is lost**. Even if extraction fails, the queue is full, or the bot is shutting down, the message is in the database.

## 4. Queue, Concurrency, and Shutdown

**Bounded asyncio.Queue (default capacity 50):**
- `put_nowait()` — non-blocking. Never blocks the Discord event loop.
- When full: warning logged with `message_id` and `channel_id` (no message content). Extraction is skipped. Message is persisted.
- Configurable via `EXTRACTION_QUEUE_CAPACITY`.

**Single background worker:**
- One `asyncio.Task` processes the queue sequentially.
- `_ensure_worker()` is called on every `process()`. If the worker is `None` or `done()`, a new one is created.
- Worker wraps each `extract()` call in `try/except` — a failure in one message does not crash the worker.
- Worker recovery: if the task dies (exception or cancellation), it is automatically restarted on the next `process()` call.

**Shutdown (`shutdown_timeout`, default 10s):**
1. `_stopped = True` — `process()` still persists but stops enqueuing.
2. `await asyncio.wait_for(self._queue.join(), timeout)` — wait for all queued items to be processed.
3. If timeout exceeded: `self._worker_task.cancel()` — safe cancellation.
4. Worker catches `CancelledError`, logs, and re-raises.

**In `__main__.py`:** `processor.shutdown()` is called in the `finally` block **before** `db.close()` — the worker needs the DB (via ContextService) for in-flight extractions.

## 5. Duplicate and Edit Handling

**Two dedup layers, complementary:**

| Layer | Key | Mark on success? | Scope |
|-------|-----|-------------------|-------|
| Collector dedup | `(message_id,)` for originals, `(message_id, edited_at)` for edits | After successful `process()` | In-memory, per-session |
| Extraction dedup | `(message_id, sha256(content))` | After successful extraction only | In-memory, per-session |
| DB UNIQUE constraint | `discord_message_id` | Always (INSERT OR IGNORE) | Persistent |

- **Unchanged message**: dedup cache skips it — no API call.
- **Edited message**: different `sha256(content)` → dedup cache allows re-extraction. DB `upsert_message` updates the row.
- **Failed extraction**: NOT marked in dedup cache → will be retried on next event.
- **Failed persistence**: NOT marked in collector dedup → will be retried on next event.

## 6. Free-Tier Safeguards

| Safeguard | Implementation | Reset behavior |
|-----------|---------------|----------------|
| **Daily request cap** (default 50) | `asyncio.Lock` around atomic check-and-increment in `ExtractionService.extract()` | In-memory, resets at midnight UTC **or on restart** (documented limitation) |
| **Concurrency limit** (default 1) | `asyncio.Semaphore(1)` — serializes API calls | N/A (always 1) |
| **Pre-filter** | Deterministic regex: skips greetings, acknowledgments, pure questions, emoji-only, short messages | N/A |
| **Dedup cache** | Bounded `OrderedDict` (10K max) — prevents re-extracting unchanged messages | In-memory, per-session |
| **No OpenAI fallback** | Gemini is the only provider. Service returns structured error on failure. | N/A |
| **No retries on quota** | `rate_limited` result is returned immediately — no retry loop | N/A |

**Race condition fix (Task #23):** The old code checked the daily cap at step 4 (outside the lock) and incremented at line 337 (also outside the lock). Under concurrent access, multiple coroutines could pass the check before any incremented. The fix wraps the check-and-increment in `async with self._daily_lock`, making it atomic. The semaphore (concurrency limit) is preserved — it serializes the actual API call, while the lock serializes the counter.

## 7. Error Handling

| Scenario | What happens | Message lost? |
|----------|-------------|---------------|
| DB persistence failure | `PersistenceProcessor.process()` raises → propagates to collector → message NOT marked in dedup → retried next event | No (retried) |
| Queue full | `QueueFull` caught → warning logged → message persisted, extraction skipped | No (persisted) |
| Gemini API error | `ExtractionService.extract()` returns `ExtractionResult` with `error_type` → worker logs warning → message already persisted | No (persisted) |
| Gemini safety block | Same as above (`error_type="safety_blocked"`) | No (persisted) |
| Parse failure | Same as above (`error_type="parse_error"`) | No (persisted) |
| No API key configured | `error_type="config_error"` → message persisted, bot continues | No (persisted) |
| Worker crash | `try/except` in worker catches it → next `process()` restarts worker via `_ensure_worker()` | No (persisted) |
| Shutdown timeout | Worker cancelled → remaining queued items abandoned → but all were already persisted | No (persisted) |

**Key invariant: `ExtractionService.extract()` never raises.** It always returns an `ExtractionResult` with `error_type`. The worker's `try/except` is a safety net for unexpected bugs.

## 8. Test Results

```
201 passed in 12.88s
```

Breakdown:
- 150 — pre-existing tests (collection, DB, context, persistence, filtering, normalization, dedup, isolation)
- 31 — extraction service tests (Task 4.1: Gemini migration, pre-filter, dedup, daily cap, safety, no fallback)
- 20 — **new** extraction pipeline integration tests (Task 5):

| # | Test class | Scenario |
|---|-----------|----------|
| 1 | TestPersistBeforeExtract | Message in DB before API call (blocking service) |
| 2 | TestPersistBeforeExtract | Persistence failure prevents enqueue |
| 3 | TestEligibleVsFiltered | Actionable message extracted |
| 4 | TestEligibleVsFiltered | Greeting skipped (no API call) |
| 5 | TestEligibleVsFiltered | Emoji-only skipped (no API call) |
| 6 | TestMissingApiKey | Message persisted without API key |
| 7 | TestApiFailureNoDataLoss | Message persisted on API error |
| 8 | TestApiFailureNoDataLoss | Worker survives API error |
| 9 | TestQueueSaturation | Full queue skips extraction, message persisted |
| 10 | TestWorkerRecovery | Worker restarts after cancellation |
| 11 | TestShutdown | Shutdown drains queue |
| 12 | TestShutdown | Empty queue shutdown returns immediately |
| 13 | TestShutdown | After shutdown, process() persists but doesn't enqueue |
| 14 | TestShutdown | Timeout cancels worker |
| 15 | TestDailyCapConcurrency | 5 messages, cap=2 → only 2 API calls |
| 16 | TestEditedMessages | Edited message re-extracted (dedup allows) |
| 17 | TestEditedMessages | Unchanged message not re-extracted (dedup skips) |
| 18 | TestNoAutoPersist | Extracted tasks NOT saved to DB tasks table |
| 19 | TestProtocolCompliance | ExtractionProcessor satisfies MessageProcessor protocol |
| 20 | TestProtocolCompliance | Initial state: queue empty, worker not running |

## 9. Manual Setup

```bash
# 1. Set environment variables in backend/.env
DISCORD_TOKEN=your_discord_bot_token
PROJECT_CHANNEL_IDS=123456789012345678
GEMINI_API_KEY=your_free_tier_key_from_aistudio.google.com

# 2. Optional tuning
EXTRACTION_QUEUE_CAPACITY=50      # increase for bursty channels
EXTRACTION_SHUTDOWN_TIMEOUT=10    # increase for slow API responses
GEMINI_DAILY_REQUEST_LIMIT=50     # courtesy cap (Google RPD is authoritative)

# 3. Run the bot
cd backend
python -m checkmates
```

The bot starts with or without `GEMINI_API_KEY`. Without it, messages are persisted but not extracted (warning logged).

## 10. Risks and Next Task

**Documented limitations:**
1. **Daily cap is in-memory only.** Resets on restart. Not a billing guarantee. Google's server-side RPD (requests per day) is the authoritative guard. To persist the counter, add a JSON file or DB table — **not done per approval**.
2. **Queue saturation = silent extraction skip.** When the queue is full, extraction is permanently skipped for that message (it will not be retried unless the message is edited). The message is persisted and a warning is logged. For a hackathon this is acceptable.
3. **No task persistence.** Extracted tasks are proposals only — not saved to the `tasks` table, no Discord notifications, no external actions. This is the next task.
4. **Worker is sequential.** One background worker processes the queue one item at a time. With `max_concurrent=1` (default), the end-to-end throughput is ~1 message per Gemini API call latency (typically 1-3 seconds). Burst traffic may fill the queue.

**Next task (suggested):**
- Persist extracted tasks as proposals in a `proposed_tasks` table (with `status="proposed"`, source message ID, extraction confidence). Surface them via a Discord slash command (e.g., `/tasks review`) for the project lead to accept or reject. This bridges the gap between AI extraction and actionable task management without automatic commitment.
