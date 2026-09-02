"""Versioned schema migrations.

Migrations are deliberately written against raw SQL and raw JSON, never against
the application's Pydantic models. A migration is frozen at the moment it ships;
the models keep evolving. Coupling the two would mean an old migration silently
changing meaning the next time a model gained a field.

Every migration runs inside one transaction opened by the caller. Raising is how
a migration refuses: the caller rolls back and the database keeps its previous
version, so a failed or interrupted upgrade leaves nothing half-applied.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from app.persistence.schema import (
    BROKER_ORDERS_COLUMNS,
    BROKER_ORDERS_V1_COLUMNS,
    INDEX_STATEMENTS,
)


class MigrationError(RuntimeError):
    """A schema migration refused to proceed. The transaction is rolled back."""


def migrate_to_v2(connection: sqlite3.Connection) -> None:
    """v1 -> v2: backfill proposal instrument type, rebuild broker_orders."""

    _backfill_proposal_instrument_type(connection)
    _rebuild_broker_orders(connection)
    for statement in INDEX_STATEMENTS:
        connection.execute(statement)


# Ordered registry. The key is the version the step produces.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: migrate_to_v2,
}


# --------------------------------------------------------------------------
# v2 step 1 — legacy proposal backfill
# --------------------------------------------------------------------------
def _backfill_proposal_instrument_type(connection: sqlite3.Connection) -> None:
    """Stamp ``instrument_type: "equity"`` onto proposals written before options.

    Only the missing key is added. Nothing else about the row is touched, so a
    proposal's meaning, identity, and timestamps survive untouched. Re-running
    is a no-op because rows that already carry the key are skipped.
    """

    rows = connection.execute("SELECT proposal_id, payload_json FROM proposals").fetchall()
    for row in rows:
        proposal_id = row["proposal_id"]
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise MigrationError(
                f"Proposal {proposal_id!r} holds malformed JSON and cannot be migrated. "
                "No row was rewritten."
            ) from exc

        if not isinstance(payload, dict):
            raise MigrationError(
                f"Proposal {proposal_id!r} does not hold a JSON object and cannot be "
                "migrated. No row was rewritten."
            )

        existing = payload.get("instrument_type")
        if existing is not None:
            if existing not in ("equity", "option"):
                raise MigrationError(
                    f"Proposal {proposal_id!r} declares unknown instrument_type "
                    f"{existing!r}. No row was rewritten."
                )
            continue

        payload["instrument_type"] = "equity"
        connection.execute(
            "UPDATE proposals SET payload_json = ? WHERE proposal_id = ?",
            (_canonical_json(payload), proposal_id),
        )


def _canonical_json(payload: dict) -> str:
    """Match the repository's canonical serialization exactly."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# v2 step 2 — broker_orders rebuild
# --------------------------------------------------------------------------
def _rebuild_broker_orders(connection: sqlite3.Connection) -> None:
    """Relax NOT NULL on symbol/side and add the options columns.

    SQLite cannot drop a NOT NULL constraint in place, so this follows the
    documented table-rebuild procedure. Foreign keys are disabled by the caller
    for the duration and re-checked before the transaction commits.
    """

    if not _table_exists(connection, "broker_orders"):
        connection.execute(f"CREATE TABLE broker_orders ({BROKER_ORDERS_COLUMNS})")
        return

    if _has_column(connection, "broker_orders", "legs_json"):
        return  # already rebuilt; the step is idempotent

    before = _row_count(connection, "broker_orders")
    columns = ", ".join(BROKER_ORDERS_V1_COLUMNS)

    connection.execute("DROP TABLE IF EXISTS broker_orders_migration_v2")
    connection.execute(f"CREATE TABLE broker_orders_migration_v2 ({BROKER_ORDERS_COLUMNS})")
    connection.execute(
        f"INSERT INTO broker_orders_migration_v2({columns}) "
        f"SELECT {columns} FROM broker_orders"
    )

    copied = _row_count(connection, "broker_orders_migration_v2")
    if copied != before:
        raise MigrationError(
            f"broker_orders rebuild copied {copied} of {before} rows; migration aborted."
        )

    connection.execute("DROP TABLE broker_orders")
    connection.execute("ALTER TABLE broker_orders_migration_v2 RENAME TO broker_orders")

    after = _row_count(connection, "broker_orders")
    if after != before:
        raise MigrationError(
            f"broker_orders holds {after} rows after rebuild but held {before} before; "
            "migration aborted."
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
