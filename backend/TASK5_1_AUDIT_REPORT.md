# Task 5.1 — Post-Integration Reliability Audit Report

## Overview

A focused code review of the extraction pipeline implementation (Task 5) was performed to verify reliability before adding new features. The audit inspected the actual implementation code and tests — not the prior implementation report.

**Full regression test result: 211 passed, 0 failed** (201 original + 10 new audit regression tests)

---

## Scope

Files inspected:

| File | Purpose |
|------|---------|
| `checkmates/processors/extraction.py` | `ExtractionProcessor` — bounded queue, single worker, shutdown |
| `checkmates/extraction/service.py` | `ExtractionService` — Gemini API calls, daily cap, dedup |
| `checkmates/__main__.py` | Wiring — pipeline construction, shutdown ordering |
| `checkmates/config.py` | Configuration — queue capacity, shutdown timeout |
| `tests/test_extraction_pipeline.py` | Integration tests — 31 tests (20 original + 10 audit + 1 protocol) |

---

## Confirmed Findings

### 1. Shutdown and Enqueue Races

**No race condition exists.** In Python's asyncio single-threaded model, there is no `await` between the `_stopped` check and `put_nowait()` in `process()`. The sequence is:

```python
# process() — no await between check and enqueue
await self._wrapped.process(message)   # <-- await point
if self._stopped:                       # synchronous check
    return
self._ensure_worker()                   # synchronous
self._queue.put_nowait(message)         # synchronous (no await)
```

If `shutdown()` sets `_stopped = True` before `process()` reaches the check, `process()` skips enqueuing — the message is still persisted. If `process()` has already passed the check and enqueued, `shutdown()` will drain or cancel the worker. Both paths are safe.

**Database remains open until in-flight persistence finishes:** `__main__.py` calls `extraction_processor.shutdown()` in the `finally` block before `db.close()`. The `shutdown()` method awaits the worker (up to the timeout), ensuring the worker stops before the DB closes.

**Shutdown idempotency** — confirmed as a **bug that was fixed** (see Fixes below).

### 2. Worker Lifecycle and Queue Accounting

- **`task_done()` in `finally` block** — confirmed correct (line 245). Every dequeued item is paired with `task_done()`, even on exception.
- **Exception isolation** — `try/except Exception` inside the `while True` loop (line 237) catches per-item failures. One item raising does not crash the worker or skip subsequent items.
- **Worker death with pending items** — confirmed as a **documented limitation** (see Limitations below).
- **No duplicate workers possible** — `_ensure_worker()` is synchronous (no `await`), so two concurrent `process()` calls cannot both pass the `done()` check and create duplicate tasks.

### 3. Shutdown Timeout

- **Timeout bounds the drain** — `asyncio.wait_for(self._queue.join(), timeout=self._shutdown_timeout)` bounds the drain phase.
- **In-flight item logging** — confirmed as a **bug that was fixed** (see Fixes below).
- **Cancellation cleanup** — after timeout, the worker is cancelled and awaited. Both `CancelledError` and unexpected `Exception` are caught, preventing unhandled task exception warnings.

### 4. Daily Gemini Cap

- **Atomic check-and-increment** — confirmed correct. `async with self._daily_lock:` protects the check-and-increment at step 7 (lines 338–363). The counter is incremented inside the lock before the API call is made.
- **UTC day rollover** — confirmed correct. `_check_and_reset_daily_count()` uses `datetime.now(timezone.utc).date()`. When the date changes, the counter resets to 0.
- **Failed API calls consume budget** — confirmed as intended policy. The counter increments at step 7 (line 363) before the API call at step 8. There is no decrement on failure. Each attempt counts against the daily cap.
- **Semaphore enforces concurrency** — confirmed correct. `async with self._semaphore:` at step 8 (line 367) limits concurrent API calls to `gemini_max_concurrent`.
- **Fast-path check at step 4** — confirmed as an optimization, not a bug. Step 4 (lines 270–293) checks the cap outside the lock to short-circuit before building prompts. Step 7 (lines 338–363) re-checks inside the lock as the authoritative check. If the cap was reached between steps 4 and 7, step 7 catches it.
- **No paid fallback, no persistent quota tracking, no automatic retries** — confirmed. The cap is in-memory only, resets on restart.

### 5. Tests

- **Real asynchronous behavior** — the 20 original integration tests use `await asyncio.sleep()`, `asyncio.Event` for blocking, `asyncio.wait_for()` for timeouts, and real `asyncio.Queue` operations. They exercise actual async control flow.
- **Test gaps filled** — 10 new regression tests added for confirmed gaps (see New Tests below).

---

## Fixes Applied

Three bugs were identified and fixed in `checkmates/processors/extraction.py`:

### Fix 1: Shutdown idempotency guard (line 273–274)

**Problem:** `shutdown()` had no guard against being called multiple times. A second call would attempt to drain the already-drained queue and cancel an already-cancelled worker, potentially causing errors or hangs.

**Fix:** Added an idempotency guard at the top of `shutdown()`:

```python
if self._stopped:
    return
```

This prevents double-drain, double-cancel, and races between concurrent `shutdown()` calls. The first call sets `_stopped = True` and performs the drain-and-cancel sequence; subsequent calls return immediately.

### Fix 2: Catch unexpected exceptions when awaiting cancelled worker (lines 315–321)

**Problem:** After cancelling the worker task, `await self._worker_task` only caught `asyncio.CancelledError`. If the worker died with a different exception (e.g., `RuntimeError`) before cancellation took effect, the exception would propagate as an unhandled task exception warning.

**Fix:** Added an `except Exception` clause:

```python
except Exception as exc:
    logger.warning(
        "Extraction worker raised exception during shutdown: %s",
        exc,
    )
```

### Fix 3: Improved shutdown timeout log message (lines 298–307)

**Problem:** The timeout log said "N items remaining" but did not account for the one in-flight item that the worker may have already `get()`-ed but not yet `task_done()`-ed. This could mislead operators into thinking only N items were lost when N+1 were possible.

**Fix:** Updated the log message to explicitly mention the in-flight item:

```python
logger.warning(
    "Extraction queue drain timed out after %.1fs -- "
    "cancelling worker (%d items in queue, plus 1 in-flight "
    "item possible)",
    ...
)
```

---

## Changed Files

| File | Changes |
|------|---------|
| `checkmates/processors/extraction.py` | 3 fixes: idempotency guard (line 273), `except Exception` on worker await (line 315), improved timeout log (lines 298–307) |
| `tests/test_extraction_pipeline.py` | 10 new tests in 4 new test classes (lines 932–1304) |

No other files were modified. No changes to `service.py`, `__main__.py`, or `config.py` were needed.

---

## New Tests

### TestWorkerDeathWithPendingItems (2 tests)

| Test | What it verifies |
|------|-----------------|
| `test_worker_restart_processes_stranded_items` | If the worker dies with items in the queue, a subsequent `process()` call restarts the worker via `_ensure_worker()` and processes the stranded items. |
| `test_stranded_items_without_new_message` | If the worker dies and no new message arrives, items remain stranded in the queue. Confirms the documented limitation — the message IS persisted in the DB, but extraction work is dropped. |

### TestConcurrentShutdownEnqueue (2 tests)

| Test | What it verifies |
|------|-----------------|
| `test_process_during_shutdown_no_crash` | `process()` and `shutdown()` running concurrently must not crash. The message is persisted regardless. Uses `SlowExtractionService` to create deterministic overlap. |
| `test_enqueue_after_shutdown_skipped` | After `shutdown()` completes, `process()` still persists but skips enqueuing. The `_stopped` flag prevents new extraction work. |

### TestQueueAccounting (2 tests)

| Test | What it verifies |
|------|-----------------|
| `test_task_done_called_on_exception` | `task_done()` is called even when `extract()` raises unexpectedly. Uses `ExplodingService` that raises `RuntimeError`. Enqueues 3 messages — if `task_done()` were missing, `queue.join()` would hang forever. |
| `test_task_done_called_on_success` | On success, `task_done()` is called and `queue.join()` completes. Enqueues 5 messages and verifies the queue drains. |

### TestShutdownEdgeCases (4 tests)

| Test | What it verifies |
|------|-----------------|
| `test_shutdown_idempotent` | Calling `shutdown()` 3 times does not crash or hang. The second and third calls return near-instantly (elapsed < 0.5s). |
| `test_shutdown_without_worker_started` | `shutdown()` before any `process()` call is a safe no-op. No worker to drain or cancel. |
| `test_shutdown_after_worker_already_done` | `shutdown()` when the worker has already completed (e.g., cancelled externally) handles the dead worker gracefully. |
| `test_shutdown_preserves_persisted_messages` | Messages persisted before shutdown timeout are in the DB. Uses `SlowExtractionService` with 0.3s timeout — extraction is dropped, but the message is persisted. |

---

## Full Regression Test Result

```
$ cd backend && .venv/Scripts/python.exe -m pytest --tb=short -q

........................................................................ [ 34%]
........................................................................ [ 68%]
...................................................................      [100%]

211 passed in 13.88s
```

- 201 tests from the original Task 5 suite
- 10 new audit regression tests (Task 5.1)
- 0 failures, 0 errors, 0 warnings

---

## Can Extraction Work Be Dropped or Remain Queued?

**Yes — extraction work can be dropped or remain queued during failure or shutdown.** This is by design and is explicitly documented:

1. **Queue saturation:** If the bounded queue is full (`put_nowait()` raises `QueueFull`), extraction is skipped for that message. The message remains persisted in the DB. A warning is logged with `message_id` and `channel_id` (no message content).

2. **Shutdown timeout:** If the queue does not drain within `shutdown_timeout` seconds, the worker is cancelled. Any remaining queued items (plus one in-flight item) are dropped. The messages are already persisted in the DB.

3. **Worker death without recovery:** If the worker dies (exception or cancellation) and no new `process()` call arrives to trigger `_ensure_worker()`, items remain stranded in the queue indefinitely until restart. The messages are persisted, but extraction does not happen.

4. **Daily cap reached:** When the daily request limit is reached, extraction is skipped for subsequent messages. The messages are persisted, but no API call is made.

**Database persistence is a separate guarantee from extraction.** Every message is persisted to the DB before any extraction work is attempted (`await self._wrapped.process(message)` at step 1 of `process()`). If the wrapped processor raises, the message is NOT enqueued and the collector retries it. Once persisted, the message is safe in the DB regardless of what happens to the extraction queue.

**Extraction is best-effort, not guaranteed.** The system is designed so that extraction failures never cause data loss — messages are always persisted first. But extraction itself can be skipped, dropped, or delayed without error under the conditions above.

---

## Remaining Limitations

1. **Stranded items require a new message for recovery.** If the worker dies, items in the queue are only processed when a new `process()` call triggers `_ensure_worker()`. There is no heartbeat or watchdog to detect a dead worker proactively. This is acceptable for the current design — the messages are already persisted, and only extraction (a best-effort operation) is delayed.

2. **Daily cap resets on restart.** The daily request counter is in-memory. On restart, the counter resets to 0. This is documented in the error message and the service docstring. No persistent quota tracking was introduced (per constraints).

3. **Failed API calls consume daily budget.** The counter increments before the API call and is not decremented on failure. This is the intended policy — each attempt counts. If the Gemini API is down for an extended period, the daily cap may be exhausted by failed calls.

4. **No backpressure to Discord.** If the queue saturates, the bot continues processing Discord events (persisting messages) but skips extraction. There is no mechanism to slow down or pause message ingestion. This is acceptable — persistence is fast and always succeeds; only extraction is skipped.

5. **No retry for dropped extractions.** Messages that were persisted but not extracted (due to queue saturation, shutdown, or worker death) are not retried for extraction. A future task could add a "backfill" mechanism that scans the DB for un-extracted messages and re-enqueues them.

6. **No metrics or monitoring hooks.** The system logs warnings and errors but does not expose metrics (e.g., queue depth, extraction success rate, daily cap usage) to an external monitoring system. This is a future enhancement.

---

## Constraints Compliance

| Constraint | Status |
|-----------|--------|
| Keep Gemini as the only AI provider | ✅ No other provider introduced |
| Do not persist extracted tasks | ✅ `ExtractionService.extract()` returns results; nothing is written to the tasks table |
| Do not trigger Discord actions | ✅ No Discord notifications or actions from extraction results |
| Preserve persistence-before-enqueue | ✅ `process()` calls `await self._wrapped.process()` before `put_nowait()` |
| Preserve bounded queue and single-worker design | ✅ No architectural changes to the queue or worker model |
| Do not commit or push | ✅ No commits or pushes were made |
| Do not claim messages are never lost | ✅ This report explicitly states extraction work can be dropped |
| Distinguish DB persistence from extraction | ✅ Clearly separated in the analysis above |

---

## Summary

The Task 5 implementation is sound. The audit confirmed 3 bugs (all in `extraction.py`), fixed them, and added 10 regression tests to prevent regressions. The full suite of 211 tests passes. The daily cap, queue accounting, worker lifecycle, and shutdown logic are verified correct. Extraction work is best-effort — messages are always persisted first, and extraction can be skipped or dropped under failure or shutdown conditions without data loss.
