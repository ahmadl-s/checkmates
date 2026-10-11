"""Database schema DDL statements for SQLite.

All tables use IF NOT EXISTS so initialization is idempotent.
Timestamps are stored as ISO 8601 TEXT strings (SQLite has no native datetime type).
Discord snowflake IDs are stored as INTEGER (fits in 64-bit signed int).
"""

SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(server_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        discord_user_id INTEGER NOT NULL,
        display_name TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        UNIQUE(project_id, discord_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_message_id INTEGER NOT NULL,
        server_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        author_display_name TEXT,
        content TEXT NOT NULL,
        message_timestamp TEXT NOT NULL,
        edited_timestamp TEXT,
        processed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(discord_message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        assignee_id INTEGER,
        deadline TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        source_message_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id INTEGER NOT NULL,
        prerequisite_task_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (task_id, prerequisite_task_id),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (prerequisite_task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        CHECK (task_id != prerequisite_task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        server_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        UNIQUE(channel_id)
    )
    """,
]

# Indexes for common query patterns
INDEX_STATEMENTS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_messages_server_channel ON messages(server_id, channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_processed ON messages(processed_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_members_project ON members(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_members_discord_user ON members(discord_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_channels_channel ON project_channels(channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_channels_project ON project_channels(project_id)",
    # Partial unique index: prevents duplicate tasks from the same source
    # message (same source_message_id + title). Only applies when
    # source_message_id is NOT NULL, so manually created tasks are unaffected.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_title "
    "ON tasks(source_message_id, title) WHERE source_message_id IS NOT NULL",
]
