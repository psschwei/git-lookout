from __future__ import annotations

import sqlite3
from pathlib import Path

# The complete schema, applied to a fresh database. Each statement is idempotent
# (CREATE TABLE / INDEX IF NOT EXISTS) so connecting to an existing database is a
# no-op. There is no ORM — raw SQL over the four tables described in docs/spec.md.
#
# The schema lives in a single string per the v1 "migration setup" approach: bump
# SCHEMA_VERSION and add an entry to MIGRATIONS when the shape needs to change.
SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    installation_id INTEGER NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    UNIQUE(owner, name)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    title TEXT,
    author TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, pr_number)
);

CREATE TABLE IF NOT EXISTS pr_files (
    pr_id INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    PRIMARY KEY (pr_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_pr_files_path ON pr_files(file_path);

CREATE TABLE IF NOT EXISTS conflict_checks (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    pr_a_number INTEGER NOT NULL,
    pr_b_number INTEGER NOT NULL,
    pr_a_sha TEXT NOT NULL,
    pr_b_sha TEXT NOT NULL,
    status TEXT NOT NULL,       -- 'conflict' | 'clean'
    conflicting_files TEXT,     -- JSON array, e.g. '["src/api/orders.ts"]'
    comment_id_a INTEGER,       -- GitHub comment ID on PR A
    comment_id_b INTEGER,       -- GitHub comment ID on PR B
    checked_at TEXT NOT NULL,
    UNIQUE(repo_id, pr_a_number, pr_b_number)
);
"""

# Current schema version. Stored in the database via PRAGMA user_version so the
# migration runner can tell how far a database has been brought up to date.
SCHEMA_VERSION = 1


def connect(db_path: str | Path) -> sqlite3.Connection:
    """
    Open a connection to the SQLite database and apply the schema.

    Foreign keys are enabled per-connection (SQLite defaults them off), so the
    ON DELETE CASCADE on pr_files actually fires. Row access is by column name.
    The schema is applied on every connect — it is idempotent, so this is safe
    for both fresh and existing databases.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """
    Bring the database up to the current schema version.

    v1 has a single migration: create the four tables. The version is tracked in
    PRAGMA user_version. Future schema changes append to this function, guarded by
    the current version so each migration runs exactly once.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < 1:
        conn.executescript(SCHEMA)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
