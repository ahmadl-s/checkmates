"""Async SQLite database connection management.

Uses a single persistent connection with WAL mode and foreign-key enforcement.
All operations go through this one connection, avoiding per-query connection
overhead. The connection runs in a background thread (managed by aiosqlite).
"""

from __future__ import annotations

import logging

import aiosqlite

from checkmates.database.schema import INDEX_STATEMENTS, SCHEMA_STATEMENTS

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database wrapper.

    Lifecycle:
        db = Database("path/to/db.sqlite")
        await db.init()       # open connection, create schema
        ... use db ...
        await db.close()      # close connection
    """

    def __init__(self, db_path: str = "checkmates.db") -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the connection, enable pragmas, and create all tables/indexes."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        # Enable WAL mode for concurrent read/write access
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Enforce foreign-key constraints (off by default in SQLite)
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

        # Create schema
        for statement in SCHEMA_STATEMENTS:
            await self._conn.execute(statement)
        for statement in INDEX_STATEMENTS:
            await self._conn.execute(statement)
        await self._conn.commit()

        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        """Return the active connection, raising if not initialized."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement and return the cursor."""
        return await self.connection.execute(sql, params)

    async def executemany(self, sql: str, params: list[tuple]) -> aiosqlite.Cursor:
        """Execute a SQL statement with multiple parameter sets."""
        return await self.connection.executemany(sql, params)

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self.connection.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        """Execute a query and return the first row (or None)."""
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """Execute a query and return all matching rows."""
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchall()
