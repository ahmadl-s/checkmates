# Checkmates Backend — Local Testing Guide

This guide walks through manually testing the Checkmates bot backend before
implementing new features. It distinguishes **what can be verified from the
code** (no live Discord or Gemini connection) from **what requires a live
test** (real Discord messages and/or Gemini API calls).

---

## 1. Backend Directory & Virtual Environment

### Project location

```
C:\Users\alawa\Videos\SOFTWARE_ENGINEERING\checkmates\backend\
```

### Virtual environment

A virtual environment already exists at `backend\.venv\` with all
dependencies installed (Python 3.13.14, checkmates installed in editable
mode).

**Activate (Git Bash / Windows Terminal):**

```bash
cd /c/Users/alawa/Videos/SOFTWARE_ENGINEERING/checkmates/backend
source .venv/Scripts/activate
```

**Activate (PowerShell):**

```powershell
cd C:\Users\alawa\Videos\SOFTWARE_ENGINEERING\checkmates\backend
.\.venv\Scripts\Activate.ps1
```

**Verify activation:**

```bash
python --version    # Should print: Python 3.13.x
which python        # Should point to .venv/Scripts/python
pip list           # Should show: checkmates 0.1.0, discord.py, google-genai, etc.
```

> **Note:** You can also use the venv Python directly without activating:
> `.venv/Scripts/python.exe -m checkmates`

---

## 2. Dependency Installation (If Needed)

### Current state

All dependencies are already installed. The `pip list` output includes:

| Package            | Version  | Purpose                          |
|--------------------|----------|----------------------------------|
| discord.py         | 2.7.1    | Discord client                   |
| pydantic           | 2.14.0   | Data models                      |
| pydantic-settings  | 2.15.0   | .env configuration loading       |
| python-dotenv      | 1.2.4    | .env file support                |
| aiosqlite          | 0.22.1   | Async SQLite                     |
| google-genai       | 2.29.0   | Gemini API client                |
| pytest             | 9.1.1    | Test runner (dev)                |
| pytest-asyncio     | 1.4.0    | Async test support (dev)         |

### If you ever need to reinstall

From the `backend\` directory with the venv activated:

```bash
# Install runtime + dev dependencies from pyproject.toml (no upgrades)
pip install -e ".[dev]"

# Or install only runtime dependencies
pip install -e .
```

> **Do not run `pip install -U` or `pip install --upgrade`** unless you
> intend to upgrade packages. The pinned versions are tested and working.
> A `.lock` file is not used; `pyproject.toml` specifies minimum versions.

### Run the test suite (verifies dependencies without Discord/Gemini)

```bash
.venv/Scripts/python.exe -m pytest --tb=short -q
```

Expected: **211 passed** (all tests use mocked Gemini — no API calls).

---

## 3. Required .env Variables

The configuration is defined in `checkmates/config.py` (a
`pydantic-settings.BaseSettings` subclass) and loaded from `backend\.env`.

### Currently configured variables (values not shown for security)

| Variable                       | Status     | Required?                |
|--------------------------------|------------|--------------------------|
| `DISCORD_TOKEN`                | **SET**    | Required — bot won't start without it |
| `PROJECT_CHANNEL_IDS`          | **SET**    | Required — comma-separated Discord channel IDs to monitor |
| `DATABASE_PATH`                | SET        | Optional — default: `checkmates.db` |
| `LOG_LEVEL`                    | SET        | Optional — default: `INFO` |
| `TIMEZONE`                     | SET        | Optional — default: `UTC` |
| `CONTEXT_MAX_MESSAGES`         | SET        | Optional — default: `50` |
| `GEMINI_API_KEY`               | **EMPTY**  | Optional for bot startup; **required for AI extraction** |
| `GEMINI_MODEL`                 | SET        | Optional — default: `gemini-2.5-flash-lite` |
| `GEMINI_TIMEOUT`               | SET        | Optional — default: `30` (seconds) |
| `GEMINI_MAX_CONCURRENT`        | SET        | Optional — default: `1` |
| `GEMINI_DAILY_REQUEST_LIMIT`   | SET        | Optional — default: `50` |
| `EXTRACTION_MIN_MESSAGE_LENGTH`| SET        | Optional — default: `10` |
| `EXTRACTION_QUEUE_CAPACITY`    | not set    | Optional — default: `50` |
| `EXTRACTION_SHUTDOWN_TIMEOUT`  | not set    | Optional — default: `10` (seconds) |

### Where to get the required values

**DISCORD_TOKEN:**
1. Go to https://discord.com/developers/applications
2. Select (or create) your application
3. Go to **Bot** → **Token** → **Reset Token** (or copy existing)
4. Paste into `.env` as `DISCORD_TOKEN=your_token_here`

**PROJECT_CHANNEL_IDS:**
1. In Discord, enable **Developer Mode** (User Settings → Advanced → Developer Mode)
2. Right-click the channel you want to monitor → **Copy ID**
3. Paste as a comma-separated list: `PROJECT_CHANNEL_IDS=123456789012345678,987654321098765432`

**GEMINI_API_KEY (for extraction only):**
1. Go to https://aistudio.google.com/apikey
2. Create an API key
3. Paste into `.env` as `GEMINI_API_KEY=your_key_here`
4. **Cost safeguard:** To guarantee zero spending, use a Google Cloud project
   that has NOT linked a billing account and remains on the Free tier.

### .env file location

The `.env` file is at `backend\.env` (gitignored, never committed). The
template is `backend\.env.example` — copy it if starting fresh:

```bash
cp .env.example .env
# Then edit .env with your actual values
```

> **Never print or share your actual token or API key.** This guide does not
> expose them.

---

## 4. Discord Bot Permissions & Invite Configuration

### Required Discord Developer Portal settings

The bot (`MessageCollector` in `checkmates/collector.py`) requests these
intents:

```python
intents = discord.Intents.default()
intents.message_content = True   # Privileged intent — required to read message text
intents.members = True           # Privileged intent — required for author.display_name
```

**In the Discord Developer Portal** (https://discord.com/developers/applications):

1. Select your application → **Bot**
2. Under **Privileged Gateway Intents**, enable:
   - ✅ **MESSAGE CONTENT INTENT**
   - ✅ **SERVER MEMBERS INTENT**
   - (Presence Intent is NOT required)
3. Under **Authorization Method**, use **URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Read Message History`
4. Copy the generated invite URL and open it in a browser to invite the bot
   to your server.

### Minimum required permissions (integer permission value: 66560)

| Permission              | Why needed                              |
|-------------------------|-----------------------------------------|
| View Channels            | See the configured project channels      |
| Read Message History    | Receive `on_message` events              |
| Message Content (intent)| Read the text of messages                |
| Server Members (intent) | Resolve author display names             |

### Verifying the bot is in the right server

- The bot only processes messages from channels listed in
  `PROJECT_CHANNEL_IDS`. Messages from other channels are silently ignored
  by `MessageFilter.should_process()`.
- The channel IDs must match channels in a server where the bot has been
  invited and has the above permissions.

### Code-verifiable (no live test needed)

- Intent configuration is hardcoded in `collector.py` lines 50-53.
- The filter logic (bot rejection, channel allowlist, empty content) is in
  `filtering.py` — fully testable without a live Discord connection.
- The 211-test suite already covers these paths with mocked Discord objects.

---

## 5. Running the Backend

### Start command (from the `backend\` directory)

```bash
.venv/Scripts/python.exe -m checkmates
```

Or after activating the venv:

```bash
python -m checkmates
```

### What the startup sequence does (from `__main__.py`)

1. Loads `Settings` from `.env`
2. Exits immediately if `DISCORD_TOKEN` is empty
3. Exits immediately if `PROJECT_CHANNEL_IDS` is empty
4. Initializes the SQLite database (`Database.init()`) — creates tables, WAL mode, FK enforcement
5. Constructs the pipeline:
   ```
   ContextService → ExtractionPrefilter → ExtractionService → ExtractionProcessor(wraps PersistenceProcessor) → MessageCollector
   ```
6. Logs whether extraction is enabled or disabled based on `GEMINI_API_KEY`
7. Starts the Discord client (`client.start(discord_token)`)
8. On shutdown (Ctrl+C or error): drains extraction queue, then closes DB

### Expected startup log output

**With GEMINI_API_KEY set:**
```
[INFO] checkmates: AI extraction ENABLED: model=gemini-2.5-flash-lite, daily_limit=50, max_concurrent=1, queue_capacity=50
[INFO] checkmates: Starting checkmates bot. Monitoring N channel(s). Database: checkmates.db
```

**Without GEMINI_API_KEY (current state):**
```
[WARNING] checkmates: AI extraction DISABLED: GEMINI_API_KEY is not set. Messages will be persisted but not extracted. Set GEMINI_API_KEY in .env to enable extraction.
[INFO] checkmates: Starting checkmates bot. Monitoring N channel(s). Database: checkmates.db
```

### Stopping the bot

Press **Ctrl+C**. The `finally` block in `__main__.py` will:
1. Call `extraction_processor.shutdown()` — drains the queue (up to 10s), cancels worker
2. Call `db.close()` — closes the SQLite connection

---

## 6. Verifying Message Collection, Filtering, and Persistence

### What happens when a Discord message arrives (pipeline flow)

```
Discord on_message event
  → MessageFilter.should_process()     [filtering.py]
      • Rejects bot-authored messages
      • Rejects messages from non-configured channels
      • Rejects empty messages (embeds/stickers without text)
  → DeduplicationCache.is_seen_original()  [dedup.py]
      • In-memory check — was this message_id already processed?
  → MessageNormalizer.normalize()      [normalizer.py]
      • Converts discord.Message → NormalizedMessage (frozen Pydantic model)
  → ExtractionProcessor.process()      [processors/extraction.py]
      → PersistenceProcessor.process() [processors/persistence.py]
          → MessageRepository.upsert_message() [database/repository.py]
              • Original: INSERT OR IGNORE into messages table
              • Edit: UPDATE existing row (or insert if missing)
      → Queue.put_nowait(message)  [enqueue for background extraction]
  → DeduplicationCache.mark_original() [mark only after successful processing]
```

### Log lines to look for

**Collection (INFO):**
```
[INFO] checkmates.collector: Forwarding message id=123456789 channel=987654321 author=111111 edit=False
```

**Persistence (INFO):**
```
[INFO] checkmates.processors.persistence: Persisted message discord_id=123456789 action=inserted edit=False
```
- `action=inserted` = new message
- `action=updated/idempotent` = duplicate (already in DB) or edit update

**Filtering (no log — filtered messages are silently skipped):**
- Messages from bots, wrong channels, or empty content are rejected by
  `should_process()` without any log line. If you want to verify filtering,
  set `LOG_LEVEL=DEBUG` — but even then, filtered messages are not logged
  (by design, to avoid log spam).

### How to verify (live test)

1. Start the bot (Section 5)
2. Send a message in one of the configured `PROJECT_CHANNEL_IDS` channels
3. Check console output for the "Forwarding message" and "Persisted message" log lines
4. Send a message in a non-configured channel — no log should appear
5. Send a message from a bot account — no log should appear

### How to verify (code-only, no live test)

Run the existing test suite:
```bash
.venv/Scripts/python.exe -m pytest tests/test_collector.py tests/test_filtering.py tests/test_normalizer.py tests/test_dedup.py --tb=short -q
```

These tests use mocked Discord message objects and verify the full
filter → dedup → normalize → persist flow without a live Discord connection.

---

## 7. Verifying Gemini Extraction

### Extraction pipeline (background worker)

When a message is enqueued, the background worker calls:

```
ExtractionService.extract(message)  [extraction/service.py]
  1. ExtractionPrefilter.should_extract()   — deterministic, no AI call
     → Skips: greetings, acknowledgments, pure questions, emoji-only, too short
     → Extracts: commitment language, task verbs, deadlines, assignments, dependencies
  2. ContextService.get_context()           — retrieves project context from DB
     → Needs project_channels table populated (see Section 7.1 below)
     → If no project found: returns error_type="no_context"
  3. Check GEMINI_API_KEY configured
     → If not set: returns error_type="config_error"
  4. Check daily request cap (in-memory, UTC reset)
     → If exceeded: returns error_type="rate_limited"
  5. Check extraction dedup cache (content hash)
     → If already extracted with same content: returns error_type="skipped"
  6. Gemini API call (semaphore-limited)
  7. Parse structured response
  8. Enrich tasks with source traceability and assignee resolution
  9. Log result
```

### 7.1 Critical: Project channels must be set up manually

The `__main__.py` startup code does **not** populate the `project_channels`
table. Without a project registered for a channel, extraction will always
return `error_type="no_context"` — the Gemini API is never called.

**To set up a project (after the bot has created the database):**

Use the venv Python to run a one-time setup script:

```python
# Run from the backend\ directory with venv activated:
# python -c "..."
import asyncio
import aiosqlite

async def setup():
    db = await aiosqlite.connect("checkmates.db")

    # Replace these with your actual IDs:
    SERVER_ID = 123456789012345678       # Your Discord server (guild) ID
    CHANNEL_ID = 987654321098765432      # The channel ID from PROJECT_CHANNEL_IDS
    PROJECT_NAME = "CS101 Group Project"  # Any name you choose

    # Create project
    await db.execute(
        "INSERT OR IGNORE INTO projects (server_id, name) VALUES (?, ?)",
        (SERVER_ID, PROJECT_NAME)
    )
    await db.commit()

    # Get project ID
    cursor = await db.execute(
        "SELECT id FROM projects WHERE server_id = ? AND name = ?",
        (SERVER_ID, PROJECT_NAME)
    )
    row = await cursor.fetchone()
    project_id = row[0]

    # Register channel under this project
    await db.execute(
        "INSERT OR IGNORE INTO project_channels (project_id, channel_id, server_id) VALUES (?, ?, ?)",
        (project_id, CHANNEL_ID, SERVER_ID)
    )
    await db.commit()

    print(f"Project '{PROJECT_NAME}' (id={project_id}) set up for channel {CHANNEL_ID}")
    await db.close()

asyncio.run(setup())
```

> **This is code-verifiable:** the `ContextService.get_context()` method in
> `context/service.py` performs a JOIN on `project_channels` — if no row
> exists for the message's `channel_id`, `project` is `None` and extraction
> is skipped.

### Log lines to look for (extraction)

**Extraction enabled (startup):**
```
[INFO] checkmates: AI extraction ENABLED: model=gemini-2.5-flash-lite, ...
```

**Enqueued for extraction (DEBUG):**
```
[DEBUG] checkmates.processors.extraction: Enqueued message_id=123456789 for extraction (queue_size=1/50)
```

**Worker started:**
```
[INFO] checkmates.processors.extraction: Extraction worker started
```

**Extraction skipped by pre-filter (INFO):**
```
[INFO] checkmates.extraction.service: Extraction skipped by pre-filter: reason=greeting message_id=123456789 channel=987654321
```
Skip reasons: `greeting`, `acknowledgment`, `pure_question`, `emoji_only`, `too_short`

**No project context (INFO):**
```
[INFO] checkmates.extraction.service: Extraction skipped: no project context for channel_id=987654321 server_id=123456789 message_id=123456789
```

**API key not configured (WARNING):**
```
[WARNING] checkmates.extraction.service: Extraction failed: GEMINI_API_KEY not configured for message_id=123456789
```

**Daily cap reached (WARNING):**
```
[WARNING] checkmates.extraction.service: Extraction skipped: daily request limit (50) reached for message_id=123456789
```

**Already extracted (dedup) (INFO):**
```
[INFO] checkmates.extraction.service: Extraction skipped: already extracted (unchanged content) message_id=123456789
```

**Gemini API error (WARNING):**
```
[WARNING] checkmates.extraction.service: Gemini API error for message_id=123456789: type=rate_limited msg=Gemini rate limit or quota exhausted: ...
```
Error types: `rate_limited`, `config_error`, `api_error`, `parse_error`, `safety_blocked`, `empty_response`

**Extraction success (INFO):**
```
[INFO] checkmates.extraction.service: Extraction complete for message_id=123456789: 3 tasks found, has_actionable_tasks=True
```
or from the ExtractionProcessor worker:
```
[INFO] checkmates.processors.extraction: Extraction completed: message_id=123456789 tasks=3 actionable=True model=gemini-2.5-flash-lite
```

**Queue full (WARNING):**
```
[WARNING] checkmates.processors.extraction: Extraction queue full (50/50) -- extraction SKIPPED for message_id=123456789 channel_id=987654321. Message is persisted but will not be extracted.
```

### How to verify (code-only, no live test, no API calls)

Run the extraction test suite (uses MockGeminiClient — zero API calls):

```bash
.venv/Scripts/python.exe -m pytest tests/test_extraction.py tests/test_extraction_pipeline.py tests/test_prefilter.py --tb=short -q
```

These tests cover:
- Pre-filter logic (all skip reasons)
- Extraction with mocked Gemini responses
- Daily cap enforcement
- Dedup cache behavior
- Queue saturation and worker recovery
- Shutdown edge cases
- Persistence-before-extraction ordering

### How to verify (live test with real Gemini)

> ⚠️ This incurs Gemini API usage. Only do this if you have a free-tier key
> and are comfortable with the request counting against your daily quota.

1. Set `GEMINI_API_KEY` in `.env`
2. Set up a project + channel mapping (Section 7.1)
3. Start the bot
4. Send a message with actionable language in the configured channel, e.g.:
   - "I'll build the API endpoint by Friday"
   - "We need to review the PR before the meeting"
5. Watch for "Extraction complete" log lines
6. Check that the daily counter is tracked (look for daily cap log if you
   exceed the limit)

### How to verify (no API key — extraction disabled)

With `GEMINI_API_KEY` empty, every eligible message will log:
```
[WARNING] checkmates.extraction.service: Extraction failed: GEMINI_API_KEY not configured for message_id=...
```
This is the expected behavior — messages are still persisted, only
extraction is skipped.

---

## 8. Inspecting the SQLite Database

### Database location

The database file is created at the path specified by `DATABASE_PATH` in
`.env`. The current value is `checkmates.db`, which resolves to
`backend\checkmates.db` (relative to the working directory when you run
the bot).

### The sqlite3 CLI is NOT available

The system does not have `sqlite3` in PATH. Use the venv Python instead.

### Database schema (6 tables)

| Table               | Purpose                                           |
|---------------------|---------------------------------------------------|
| `projects`          | Projects associated with Discord servers          |
| `members`           | Discord users registered as project members        |
| `messages`          | All collected Discord messages (persisted)         |
| `tasks`             | Tasks (manually created or future AI-extracted)    |
| `task_dependencies` | Prerequisite relationships between tasks           |
| `project_channels`  | Channel-to-project mappings                        |

### Inspecting the database with Python

Save this as a script or run interactively from `backend\`:

```python
# Run: .venv/Scripts/python.exe -c "..."
import sqlite3

db = sqlite3.connect("checkmates.db")
db.row_factory = sqlite3.Row

# List all tables
print("=== TABLES ===")
for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(f"  {row['name']}")

# Count messages
print("\n=== MESSAGE COUNT ===")
row = db.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()
print(f"  {row['cnt']} messages stored")

# Show recent messages (no content for privacy — just metadata)
print("\n=== RECENT MESSAGES (metadata only) ===")
for row in db.execute(
    "SELECT id, discord_message_id, channel_id, author_id, "
    "author_display_name, message_timestamp, is_edit = 0 as orig "
    "FROM messages ORDER BY id DESC LIMIT 10"
):
    # Note: 'content' column exists but is excluded here for privacy
    print(f"  id={row['id']} discord_id={row['discord_message_id']} "
          f"channel={row['channel_id']} author={row['author_display_name']} "
          f"time={row['message_timestamp']}")

# Show full message content (if you want to verify content persistence)
print("\n=== MESSAGE CONTENT (first 5) ===")
for row in db.execute(
    "SELECT discord_message_id, content FROM messages ORDER BY id DESC LIMIT 5"
):
    print(f"  discord_id={row['discord_message_id']}: {row['content'][:80]}...")

# Check project_channels setup
print("\n=== PROJECT CHANNELS ===")
for row in db.execute(
    "SELECT pc.channel_id, pc.server_id, p.name as project_name "
    "FROM project_channels pc JOIN projects p ON pc.project_id = p.id"
):
    print(f"  channel={row['channel_id']} server={row['server_id']} "
          f"project={row['project_name']}")

# Check tasks table (should be empty — extraction does not persist tasks)
print("\n=== TASKS ===")
row = db.execute("SELECT COUNT(*) as cnt FROM tasks").fetchone()
print(f"  {row['cnt']} tasks stored (expected: 0 — extraction does not save tasks)")

# Check WAL mode
print("\n=== DATABASE INFO ===")
row = db.execute("PRAGMA journal_mode").fetchone()
print(f"  journal_mode: {row[0]}")
row = db.execute("PRAGMA foreign_keys").fetchone()
print(f"  foreign_keys: {row[0]}")

db.close()
```

### WAL files

When the bot is running, you may see `checkmates.db-wal` and
`checkmates.db-shm` files alongside `checkmates.db`. These are WAL
(Write-Ahead Logging) files and are normal. They are merged into the
main DB on checkpoint or close.

---

## 9. Expected Limitations

### Extracted tasks are NOT automatically saved

This is the most important limitation to understand:

- The `tasks` table exists in the schema (`schema.py`)
- The `MessageRepository.create_task()` method exists (`repository.py`)
- **Nothing in the pipeline calls `create_task()`**

When Gemini extraction succeeds, the result is an `ExtractionResult`
containing `ExtractedTask` objects. These are:

- ✅ Logged to the console (INFO level)
- ✅ Returned from `ExtractionService.extract()`
- ✅ Received by the `ExtractionProcessor._worker()`
- ❌ **NOT persisted to the `tasks` table**
- ❌ **NOT sent as Discord notifications**
- ❌ **NOT used to trigger any external actions**

The extraction is **best-effort and proposal-only**. A future task would
need to wire `ExtractionResult.tasks` into `MessageRepository.create_task()`.

**Verification:** Run the DB inspection script from Section 8 after sending
actionable messages — the `tasks` table will have 0 rows.

### Extraction can be dropped

Extraction work (the background Gemini call) can be skipped or dropped
under these conditions, while the message is still persisted to the DB:

| Condition                    | What happens                                    |
|------------------------------|--------------------------------------------------|
| Queue full (50 items)        | Extraction skipped, message persisted            |
| Shutdown timeout (10s)       | Worker cancelled, remaining items dropped        |
| Worker dies, no new message  | Items stranded until next `process()` call      |
| Daily cap reached            | Extraction skipped, message persisted            |
| No project context           | Extraction skipped (no_context), message persisted |
| GEMINI_API_KEY not set       | Extraction skipped (config_error), message persisted |

**Database persistence is a separate guarantee.** Every message is
persisted via `INSERT OR IGNORE` (originals) or `UPDATE` (edits) before
extraction is enqueued. If persistence fails, the collector does not mark
the dedup cache and the message can be retried.

### Daily cap resets on restart

The daily request counter is in-memory. On restart, it resets to 0. This
is documented in the `.env.example` and the service docstring.

### Failed API calls consume daily budget

The counter increments before the API call and is not decremented on
failure. Each attempt counts against the daily cap.

### No backpressure to Discord

If the queue saturates, the bot continues processing Discord events
(persisting messages) but skips extraction. There is no mechanism to slow
down message ingestion.

### No retry for dropped extractions

Messages persisted but not extracted are not retried for extraction. A
future "backfill" mechanism could scan the DB for un-extracted messages.

### In-memory dedup caches do not persist across restarts

Both `DeduplicationCache` (collection-level) and `ExtractionDedupCache`
(extraction-level) are in-memory. On restart, all dedup state is lost. The
database `UNIQUE(discord_message_id)` constraint provides the authoritative
persistent dedup for message persistence — but extraction dedup does not
persist, so the same message may be re-extracted after a restart.

---

## 10. Safe Cleanup & Troubleshooting

### Stopping the bot

Press **Ctrl+C** in the terminal. The `finally` block will:
1. Drain the extraction queue (up to `EXTRACTION_SHUTDOWN_TIMEOUT` seconds)
2. Cancel the extraction worker
3. Close the database connection

Wait for the "Extraction worker stopped" and "Database connection closed"
log lines before closing the terminal.

### Resetting the database

If you want a fresh database (e.g., after testing):

```bash
# Stop the bot first, then:
rm -f checkmates.db checkmates.db-wal checkmates.db-shm
```

The database will be recreated on the next bot start with empty tables.

> **Do not delete the `.env` file** — it contains your bot token and channel
> IDs. Only delete the `.db` files.

### Running tests (no side effects)

```bash
# Full suite (211 tests, ~14 seconds, no Discord or Gemini calls)
.venv/Scripts/python.exe -m pytest --tb=short -q

# Specific module
.venv/Scripts/python.exe -m pytest tests/test_extraction_pipeline.py -v

# With verbose output
.venv/Scripts/python.exe -m pytest --tb=long -v
```

All tests use mocked Discord objects and a `MockGeminiClient` — zero API
calls, zero Discord connection, zero database side effects (tests use
temporary in-memory SQLite databases).

### Common issues

| Symptom | Likely cause | Fix |
|---------|------------|-----|
| Bot starts but no "Forwarding message" logs | Bot not in the server, or channel IDs are wrong | Verify `PROJECT_CHANNEL_IDS` matches channels in a server where the bot is invited |
| Bot starts but no "Persisted message" logs | `MessageFilter` is rejecting all messages | Check if the channel ID is in `PROJECT_CHANNEL_IDS` and the message is from a non-bot user |
| All extractions return `no_context` | `project_channels` table is empty | Set up a project + channel mapping (Section 7.1) |
| All extractions return `config_error` | `GEMINI_API_KEY` is not set | Set it in `.env` (or accept that extraction is disabled) |
| `ModuleNotFoundError: No module named 'pydantic'` | Wrong Python — venv not activated | Use `.venv/Scripts/python.exe` explicitly |
| `discord.LoginFailure: Improper token has been passed` | Invalid `DISCORD_TOKEN` | Regenerate the token in the Discord Developer Portal |
| `discord.PrivilegedIntentsRequired` | Intents not enabled in Developer Portal | Enable Message Content + Server Members intents (Section 4) |
| Bot hangs on shutdown | Worker stuck on Gemini API call | Wait for the 10s timeout, or press Ctrl+C again to force-kill |
| `sqlite3.OperationalError: database is locked` | Another process has the DB open | Ensure only one bot instance is running |

### Checking what the code does without running it

If you want to verify behavior without a live test, the 211-test suite
covers:

| Test file | What it covers |
|-----------|----------------|
| `test_collector.py` | Message collection, filtering, dedup, normalization |
| `test_filtering.py` | Bot rejection, channel allowlist, empty content |
| `test_normalizer.py` | Discord message → NormalizedMessage conversion |
| `test_dedup.py` | In-memory dedup cache, bounded eviction |
| `test_database.py` | DB schema, CRUD operations, idempotency |
| `test_repository.py` | Repository methods, project/channel isolation |
| `test_context.py` | Project context retrieval, server isolation |
| `test_extraction.py` | ExtractionService with mocked Gemini |
| `test_prefilter.py` | Pre-filter deterministic rules |
| `test_extraction_pipeline.py` | Full pipeline: persist → enqueue → extract (31 tests) |

---

## Summary: Code-Verifiable vs. Live-Test-Required

| Check | Code-only (tests) | Live test needed |
|-------|:-:|:-:|
| Dependency installation | ✅ `pip list` | — |
| Config loading | ✅ `Settings` instantiation | — |
| Message filtering | ✅ `test_filtering.py` | ✅ Real Discord message |
| Message normalization | ✅ `test_normalizer.py` | — |
| Message dedup | ✅ `test_dedup.py` | — |
| Message persistence | ✅ `test_database.py` | ✅ Real message → DB |
| Extraction pre-filter | ✅ `test_prefilter.py` | — |
| Extraction (mocked Gemini) | ✅ `test_extraction.py` | — |
| Extraction (real Gemini) | — | ✅ API key + live message |
| Queue & worker lifecycle | ✅ `test_extraction_pipeline.py` | — |
| Shutdown behavior | ✅ `test_extraction_pipeline.py` | — |
| Discord bot connection | — | ✅ Real token + server |
| Discord intents | — | ✅ Developer Portal config |
| Project context retrieval | ✅ `test_context.py` | ✅ DB with project_channels |
| DB schema & CRUD | ✅ `test_database.py` | — |
| Tasks NOT persisted | ✅ No `create_task` in pipeline | ✅ Check `tasks` table after extraction |
