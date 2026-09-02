from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.persistence.migrations import MIGRATIONS, MigrationError
from app.persistence.schema import (
    INDEX_STATEMENTS,
    MINIMUM_MIGRATABLE_VERSION,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
)


DEFAULT_DATABASE_PATH = Path("data/portfolio_governor.db")

SCHEMA_VERSION_DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
"""


class DatabaseError(RuntimeError):
    """A durable-state operation failed without leaking SQL or local secrets."""


class SchemaVersionError(DatabaseError):
    """The database schema cannot be used by this build of the application."""


def resolve_database_path(path: str | Path | None = None) -> Path:
    raw = str(path) if path is not None else os.getenv("APP_DB_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DATABASE_PATH


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = resolve_database_path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _open(self, *, foreign_keys: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=10,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        # PRAGMA foreign_keys is a no-op inside a transaction, so it is set here,
        # on a connection that has not yet issued any statement.
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        connection.execute("PRAGMA busy_timeout = 10000")
        if str(self.path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open()
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise DatabaseError(f"SQLite operation failed ({type(exc).__name__}).") from exc
        except Exception:
            # Any other failure must also leave durable state untouched.
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    # -- schema lifecycle ------------------------------------------------

    def initialize(self) -> None:
        """Bring the database to the current schema version.

        A fresh database is created directly at the latest version. An older
        database is migrated one version at a time. A database already at the
        current version is left alone. A database from a future version is
        refused rather than guessed at.
        """

        current = self._current_version()

        if current is None:
            self._create_latest()
            return

        if current == SCHEMA_VERSION:
            self._ensure_objects()
            return

        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {current} is newer than this application "
                f"supports (version {SCHEMA_VERSION}). Upgrade the application; "
                "no automatic downgrade exists."
            )

        if current < MINIMUM_MIGRATABLE_VERSION:
            raise SchemaVersionError(
                f"Database schema version {current} is older than the minimum "
                f"migratable version {MINIMUM_MIGRATABLE_VERSION}."
            )

        self._migrate(current)

    def _current_version(self) -> int | None:
        """Return the stamped version, or ``None`` for a database with no schema.

        A database that carries application tables but no stamped version is
        treated as v1 — that is what a build predating the version stamp left
        behind — rather than as empty, which would skip its migration.
        """

        with self.connection() as connection:
            connection.execute(SCHEMA_VERSION_DDL)
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            if row is not None and row["version"] is not None:
                return int(row["version"])
            legacy = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'proposals'"
            ).fetchone()
            return MINIMUM_MIGRATABLE_VERSION if legacy is not None else None

    def _create_latest(self) -> None:
        with self.connection() as connection:
            self._apply_objects(connection)
            self._stamp(connection, SCHEMA_VERSION)

    def _ensure_objects(self) -> None:
        """Idempotent re-run at the current version. Creates nothing that exists."""

        with self.connection() as connection:
            self._apply_objects(connection)

    @staticmethod
    def _apply_objects(connection: sqlite3.Connection) -> None:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in INDEX_STATEMENTS:
            connection.execute(statement)

    @staticmethod
    def _stamp(connection: sqlite3.Connection, version: int) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )

    def _migrate(self, current: int) -> None:
        """Apply each pending migration in its own all-or-nothing transaction.

        Foreign keys are disabled for the duration because a table rebuild drops
        and renames a table that participates in a foreign key, and re-checked
        with ``PRAGMA foreign_key_check`` before the work is committed.
        """

        for version in range(current + 1, SCHEMA_VERSION + 1):
            step = MIGRATIONS.get(version)
            if step is None:
                raise SchemaVersionError(
                    f"No migration is defined for schema version {version}."
                )

            connection = self._open(foreign_keys=False)
            try:
                connection.execute("BEGIN IMMEDIATE")
                step(connection)
                self._stamp(connection, version)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationError(
                        f"Migration to schema version {version} left "
                        f"{len(violations)} foreign-key violation(s); rolling back."
                    )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise DatabaseError(
                    f"Migration to schema version {version} failed "
                    f"({type(exc).__name__})."
                ) from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def healthy(self) -> bool:
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
                ).fetchone()
            return bool(row and row["version"] == SCHEMA_VERSION)
        except DatabaseError:
            return False
