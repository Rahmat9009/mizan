"""Phase 2 tests: schema versioning, v1 -> v2 migration, semantic immutability.

The v1 DDL is frozen inside this file rather than imported. A migration test
that builds its "old" database from the current schema module proves nothing;
it has to build the shape that actually exists on disk today.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.execution.models import BrokerOrderSnapshot, OrderLifecycleState
from app.models import GovernorDecision, TradeProposal
from app.persistence.database import Database, DatabaseError, SchemaVersionError
from app.persistence.migrations import MigrationError
from app.persistence.repositories import (
    LifecycleRepository,
    PersistenceConflictError,
    _same_immutable_payload,
    _serialize,
)
from app.persistence.schema import SCHEMA_VERSION

REAL_DATABASE = Path(__file__).parents[1] / "data" / "portfolio_governor.db"

# --------------------------------------------------------------------------
# Frozen schema v1
# --------------------------------------------------------------------------
V1_STATEMENTS = (
    "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    """CREATE TABLE proposals (
        proposal_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE portfolio_snapshots (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, captured_at TEXT NOT NULL)""",
    """CREATE TABLE market_risk_snapshots (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, captured_at TEXT NOT NULL)""",
    """CREATE TABLE risk_reports (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE ai_risk_analyses (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE governor_decisions (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, decided_at TEXT NOT NULL)""",
    """CREATE TABLE execution_authorizations (
        authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(proposal_id, created_at))""",
    """CREATE TABLE execution_results (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        client_order_id TEXT, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE broker_orders (
        client_order_id TEXT PRIMARY KEY,
        alpaca_order_id TEXT NOT NULL UNIQUE,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE RESTRICT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL NOT NULL CHECK(quantity > 0),
        lifecycle_status TEXT NOT NULL,
        broker_status TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        filled_at TEXT,
        filled_quantity REAL,
        filled_avg_price REAL,
        updated_at TEXT NOT NULL,
        paper INTEGER NOT NULL CHECK(paper = 1),
        payload_json TEXT NOT NULL)""",
    """CREATE TABLE audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        actor TEXT NOT NULL, action TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX idx_audit_proposal_order ON audit_events(proposal_id, sequence)",
    "CREATE INDEX idx_orders_proposal ON broker_orders(proposal_id, updated_at)",
    "CREATE INDEX idx_results_client_order ON execution_results(client_order_id)",
    "CREATE INDEX idx_authorizations_proposal ON execution_authorizations(proposal_id, created_at)",
)

# A proposal exactly as schema v1 wrote it: no instrument_type key.
LEGACY_PROPOSAL_ID = "faisal-legacy-aapl-001"
LEGACY_PROPOSAL_JSON = (
    '{"created_at":"2026-09-01T12:00:00+00:00","estimated_price":250.0,'
    '"invalidation_condition":"Signal reverses.","proposal_id":"faisal-legacy-aapl-001",'
    '"quantity":10,"side":"BUY","strategy_confidence":0.82,"symbol":"AAPL",'
    '"thesis":"Legacy equity thesis."}'
)

LEGACY_ORDER = {
    "client_order_id": "pgv5-legacy-order-1",
    "alpaca_order_id": "alpaca-legacy-1",
    "proposal_id": LEGACY_PROPOSAL_ID,
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 10.0,
    "lifecycle_status": "FILLED",
    "broker_status": "filled",
    "submitted_at": "2026-09-01T13:00:00+00:00",
    "filled_at": "2026-09-01T13:00:05+00:00",
    "filled_quantity": 10.0,
    "filled_avg_price": 249.75,
    "updated_at": "2026-09-01T13:00:06+00:00",
    "paper": 1,
}


def seed_v1(path: Path, *, proposal_json: str = LEGACY_PROPOSAL_JSON) -> None:
    """Build a database in the exact shape schema v1 left on disk."""

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        for statement in V1_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        connection.execute(
            "INSERT INTO proposals(proposal_id, payload_json, created_at) VALUES (?, ?, ?)",
            (LEGACY_PROPOSAL_ID, proposal_json, "2026-09-01T12:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO governor_decisions(proposal_id, payload_json, decided_at) VALUES (?, ?, ?)",
            (
                LEGACY_PROPOSAL_ID,
                json.dumps(
                    {
                        "proposal_id": LEGACY_PROPOSAL_ID,
                        "symbol": "AAPL",
                        "side": "BUY",
                        "decision": "APPROVE",
                        "original_quantity": 10,
                        "approved_quantity": 10,
                        "reason": "Legacy approval.",
                        "risk_score": 12,
                        "decided_at": "2026-09-01T12:30:00+00:00",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "2026-09-01T12:30:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO execution_results(proposal_id, client_order_id, payload_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (
                LEGACY_PROPOSAL_ID,
                LEGACY_ORDER["client_order_id"],
                '{"status":"SUBMITTED"}',
                "2026-09-01T13:00:06+00:00",
            ),
        )
        connection.execute(
            f"""INSERT INTO broker_orders({', '.join(LEGACY_ORDER)}, payload_json)
                VALUES ({', '.join('?' * len(LEGACY_ORDER))}, ?)""",
            (*LEGACY_ORDER.values(), '{"legacy":true}'),
        )
        for index in range(3):
            connection.execute(
                "INSERT INTO audit_events(event_id, proposal_id, actor, action, payload_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"event-{index}",
                    LEGACY_PROPOSAL_ID,
                    "governor",
                    f"ACTION_{index}",
                    json.dumps({"n": index}),
                    f"2026-09-01T12:3{index}:00+00:00",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def rows(path: Path, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def columns_of(path: Path, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in rows(path, f"PRAGMA table_info({table})")}


def version_of(path: Path) -> int:
    return rows(path, "SELECT MAX(version) AS v FROM schema_version")[0]["v"]


# --------------------------------------------------------------------------
# D — schema versioning
# --------------------------------------------------------------------------
def test_fresh_database_initializes_directly_to_latest(tmp_path) -> None:
    path = tmp_path / "fresh.db"
    Database(path)
    assert version_of(path) == SCHEMA_VERSION == 2
    assert rows(path, "SELECT version FROM schema_version") and len(
        rows(path, "SELECT version FROM schema_version")
    ) == 1, "a fresh database should stamp only the latest version"
    assert "legs_json" in columns_of(path, "broker_orders")


def test_seeded_v1_database_migrates_to_v2(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    seed_v1(path)
    assert version_of(path) == 1
    Database(path)
    assert version_of(path) == 2


def test_v2_initialization_is_idempotent(tmp_path) -> None:
    path = tmp_path / "repeat.db"
    seed_v1(path)
    Database(path)
    first = rows(path, "SELECT proposal_id, payload_json FROM proposals")[0]["payload_json"]
    for _ in range(3):
        Database(path)
    assert version_of(path) == 2
    assert rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"] == first
    assert len(rows(path, "SELECT version FROM schema_version")) == 2  # v1 and v2 stamps


def test_database_from_a_future_version_is_refused(tmp_path) -> None:
    path = tmp_path / "future.db"
    seed_v1(path)
    connection = sqlite3.connect(str(path))
    connection.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (99, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer than this application supports"):
        Database(path)


def test_unstamped_legacy_database_is_treated_as_v1_not_as_empty(tmp_path) -> None:
    path = tmp_path / "unstamped.db"
    seed_v1(path)
    connection = sqlite3.connect(str(path))
    connection.execute("DELETE FROM schema_version")
    connection.commit()
    connection.close()

    Database(path)
    assert version_of(path) == 2
    payload = json.loads(rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"])
    assert payload["instrument_type"] == "equity", "the migration must still have run"


def test_schema_version_error_is_a_database_error() -> None:
    assert issubclass(SchemaVersionError, DatabaseError)


# --------------------------------------------------------------------------
# A — legacy proposal backfill
# --------------------------------------------------------------------------
def test_migration_backfills_instrument_type(tmp_path) -> None:
    path = tmp_path / "backfill.db"
    seed_v1(path)
    Database(path)

    payload = json.loads(rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"])
    assert payload["instrument_type"] == "equity"


def test_backfill_preserves_identity_timestamps_and_meaning(tmp_path) -> None:
    path = tmp_path / "meaning.db"
    seed_v1(path)
    before = json.loads(LEGACY_PROPOSAL_JSON)
    Database(path)

    row = rows(path, "SELECT proposal_id, payload_json, created_at FROM proposals")[0]
    after = json.loads(row["payload_json"])
    assert row["proposal_id"] == LEGACY_PROPOSAL_ID
    assert row["created_at"] == "2026-09-01T12:00:00+00:00"
    assert after.pop("instrument_type") == "equity"
    assert after == before, "no field other than instrument_type may change"


def test_backfill_leaves_an_already_tagged_row_untouched(tmp_path) -> None:
    path = tmp_path / "tagged.db"
    tagged = json.dumps(
        {**json.loads(LEGACY_PROPOSAL_JSON), "instrument_type": "equity"},
        sort_keys=True,
        separators=(",", ":"),
    )
    seed_v1(path, proposal_json=tagged)
    Database(path)
    assert rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"] == tagged


def test_malformed_legacy_json_fails_closed_without_rewriting(tmp_path) -> None:
    path = tmp_path / "corrupt.db"
    seed_v1(path, proposal_json="{not valid json")

    with pytest.raises(MigrationError, match="malformed JSON"):
        Database(path)

    assert rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"] == "{not valid json"


def test_non_object_legacy_payload_fails_closed(tmp_path) -> None:
    path = tmp_path / "array.db"
    seed_v1(path, proposal_json="[1, 2, 3]")
    with pytest.raises(MigrationError, match="does not hold a JSON object"):
        Database(path)


def test_unknown_instrument_type_fails_closed(tmp_path) -> None:
    path = tmp_path / "unknown.db"
    payload = {**json.loads(LEGACY_PROPOSAL_JSON), "instrument_type": "future"}
    seed_v1(path, proposal_json=json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(MigrationError, match="unknown instrument_type"):
        Database(path)


def test_failed_migration_rolls_back_everything(tmp_path) -> None:
    """A refusal anywhere in the step must leave the database exactly as it was."""

    path = tmp_path / "rollback.db"
    seed_v1(path, proposal_json="{not valid json")

    with pytest.raises(MigrationError):
        Database(path)

    assert version_of(path) == 1, "the version stamp must not advance"
    broker_columns = columns_of(path, "broker_orders")
    assert "legs_json" not in broker_columns, "the table rebuild must not have committed"
    assert broker_columns["symbol"]["notnull"] == 1, "v1 constraints must still stand"
    assert rows(path, "SELECT name FROM sqlite_master WHERE name='broker_orders_migration_v2'") == []


# --------------------------------------------------------------------------
# C — broker_orders rebuild
# --------------------------------------------------------------------------
def test_broker_orders_gains_the_options_columns(tmp_path) -> None:
    path = tmp_path / "columns.db"
    seed_v1(path)
    Database(path)

    columns = columns_of(path, "broker_orders")
    for name in ("asset_class", "order_class", "underlying", "legs_json"):
        assert name in columns, f"{name} is missing after migration"
    assert columns["symbol"]["notnull"] == 0, "symbol must become nullable"
    assert columns["side"]["notnull"] == 0, "side must become nullable"
    assert columns["quantity"]["notnull"] == 1, "unrelated constraints must survive"


def test_broker_order_rows_survive_the_rebuild_unchanged(tmp_path) -> None:
    path = tmp_path / "orders.db"
    seed_v1(path)
    Database(path)

    row = rows(path, "SELECT * FROM broker_orders")[0]
    for column, expected in LEGACY_ORDER.items():
        assert row[column] == expected, f"{column} changed during the rebuild"
    assert row["payload_json"] == '{"legacy":true}'
    assert row["asset_class"] == "us_equity"
    assert row["order_class"] == "simple"
    assert row["underlying"] is None
    assert row["legs_json"] is None, "legs must never be fabricated for an equity row"


def test_rebuild_preserves_primary_key_and_unique_constraints(tmp_path) -> None:
    path = tmp_path / "constraints.db"
    seed_v1(path)
    Database(path)

    connection = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""INSERT INTO broker_orders({', '.join(LEGACY_ORDER)}, payload_json)
                    VALUES ({', '.join('?' * len(LEGACY_ORDER))}, ?)""",
                (*LEGACY_ORDER.values(), "{}"),
            )
    finally:
        connection.close()


def test_rebuild_preserves_the_foreign_key_to_proposals(tmp_path) -> None:
    path = tmp_path / "fk.db"
    seed_v1(path)
    Database(path)

    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        orphan = {**LEGACY_ORDER, "client_order_id": "orphan", "alpaca_order_id": "orphan-1"}
        orphan["proposal_id"] = "no-such-proposal"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""INSERT INTO broker_orders({', '.join(orphan)}, payload_json)
                    VALUES ({', '.join('?' * len(orphan))}, ?)""",
                (*orphan.values(), "{}"),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_rebuild_recreates_indexes(tmp_path) -> None:
    path = tmp_path / "indexes.db"
    seed_v1(path)
    Database(path)

    names = {
        row["name"]
        for row in rows(path, "SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"idx_orders_proposal", "idx_orders_underlying", "idx_audit_proposal_order"} <= names


# --------------------------------------------------------------------------
# Every other table survives untouched
# --------------------------------------------------------------------------
def test_audit_events_survive_with_content_and_order(tmp_path) -> None:
    path = tmp_path / "audit.db"
    seed_v1(path)
    Database(path)

    events = rows(path, "SELECT event_id, action, payload_json FROM audit_events ORDER BY sequence")
    assert [event["event_id"] for event in events] == ["event-0", "event-1", "event-2"]
    assert [event["action"] for event in events] == ["ACTION_0", "ACTION_1", "ACTION_2"]
    assert json.loads(events[2]["payload_json"]) == {"n": 2}


def test_governor_decisions_survive(tmp_path) -> None:
    path = tmp_path / "governor.db"
    seed_v1(path)
    Database(path)

    payload = json.loads(rows(path, "SELECT payload_json FROM governor_decisions")[0]["payload_json"])
    assert payload["decision"] == "APPROVE"
    assert payload["approved_quantity"] == 10
    assert GovernorDecision.model_validate(payload).risk_score == 12


def test_execution_results_survive(tmp_path) -> None:
    path = tmp_path / "results.db"
    seed_v1(path)
    Database(path)

    row = rows(path, "SELECT client_order_id, payload_json FROM execution_results")[0]
    assert row["client_order_id"] == LEGACY_ORDER["client_order_id"]
    assert row["payload_json"] == '{"status":"SUBMITTED"}'


# --------------------------------------------------------------------------
# B — semantic immutable comparison
# --------------------------------------------------------------------------
def equity_proposal(**overrides) -> TradeProposal:
    payload = {
        "proposal_id": LEGACY_PROPOSAL_ID,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "estimated_price": 250.0,
        "strategy_confidence": 0.82,
        "thesis": "Legacy equity thesis.",
        "invalidation_condition": "Signal reverses.",
        "created_at": "2026-09-01T12:00:00+00:00",
    }
    payload.update(overrides)
    return TradeProposal(**payload)


# The candidate side of the comparison is always what the repository would
# write today, so it is derived from the serializer rather than hand-rolled.
CANONICAL_TODAY = _serialize(equity_proposal())


def test_the_canonical_form_differs_from_the_legacy_bytes() -> None:
    """Guards the premise of every test below.

    Pydantic renders a UTC datetime with a `Z` suffix while schema v1 stored
    `+00:00`, so a byte comparison of a legacy row against today's serialization
    can never succeed. The backfill alone does not close that gap; the semantic
    comparison is what does.
    """

    assert CANONICAL_TODAY != LEGACY_PROPOSAL_JSON
    assert '"created_at":"2026-09-01T12:00:00Z"' in CANONICAL_TODAY
    assert '"created_at":"2026-09-01T12:00:00+00:00"' in LEGACY_PROPOSAL_JSON


def test_omitted_default_and_explicit_default_compare_equal() -> None:
    assert _same_immutable_payload(TradeProposal, LEGACY_PROPOSAL_JSON, CANONICAL_TODAY)
    explicit = LEGACY_PROPOSAL_JSON.replace("{", '{"instrument_type":"equity",', 1)
    assert _same_immutable_payload(TradeProposal, explicit, CANONICAL_TODAY)


def test_key_order_difference_compares_equal() -> None:
    reordered = json.dumps(json.loads(LEGACY_PROPOSAL_JSON), sort_keys=False)
    assert _same_immutable_payload(TradeProposal, reordered, CANONICAL_TODAY)


def test_whitespace_difference_compares_equal() -> None:
    spaced = json.dumps(json.loads(LEGACY_PROPOSAL_JSON), indent=4)
    assert _same_immutable_payload(TradeProposal, spaced, CANONICAL_TODAY)


def test_timezone_spelling_difference_compares_equal() -> None:
    zulu = LEGACY_PROPOSAL_JSON.replace("+00:00", "Z")
    assert _same_immutable_payload(TradeProposal, zulu, CANONICAL_TODAY)


def test_quantity_change_is_not_equal() -> None:
    changed = json.dumps({**json.loads(LEGACY_PROPOSAL_JSON), "quantity": 11})
    assert not _same_immutable_payload(TradeProposal, changed, CANONICAL_TODAY)


def test_side_change_is_not_equal() -> None:
    changed = json.dumps({**json.loads(LEGACY_PROPOSAL_JSON), "side": "SELL"})
    assert not _same_immutable_payload(TradeProposal, changed, CANONICAL_TODAY)


def test_price_change_is_not_equal() -> None:
    changed = json.dumps({**json.loads(LEGACY_PROPOSAL_JSON), "estimated_price": 251.0})
    assert not _same_immutable_payload(TradeProposal, changed, CANONICAL_TODAY)


def test_thesis_change_is_not_equal() -> None:
    changed = json.dumps({**json.loads(LEGACY_PROPOSAL_JSON), "thesis": "A different thesis."})
    assert not _same_immutable_payload(TradeProposal, changed, CANONICAL_TODAY)


def test_unparseable_stored_payload_is_not_equal() -> None:
    assert not _same_immutable_payload(TradeProposal, "{broken", LEGACY_PROPOSAL_JSON)


def test_a_stored_option_structure_never_matches_an_equity_proposal() -> None:
    stored_option = json.dumps(
        {
            "instrument_type": "option",
            "proposal_id": LEGACY_PROPOSAL_ID,
            "underlying": "AAPL",
            "strategy": "LONG_CALL",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert not _same_immutable_payload(TradeProposal, stored_option, LEGACY_PROPOSAL_JSON)


# --------------------------------------------------------------------------
# The critical regression: legacy proposal re-evaluation
# --------------------------------------------------------------------------
def test_legacy_proposal_reads_through_the_repository(tmp_path) -> None:
    path = tmp_path / "read.db"
    seed_v1(path)
    repository = LifecycleRepository(Database(path))

    proposal = repository.get_proposal(LEGACY_PROPOSAL_ID)
    assert proposal is not None
    assert proposal.symbol == "AAPL"
    assert proposal.quantity == 10
    assert proposal.instrument_type == "equity"


def test_legacy_proposal_can_be_re_evaluated_without_conflict(tmp_path) -> None:
    path = tmp_path / "reeval.db"
    seed_v1(path)
    repository = LifecycleRepository(Database(path))

    proposal = repository.get_proposal(LEGACY_PROPOSAL_ID)
    repository.save_proposal(proposal)  # must not raise
    repository.save_proposal(equity_proposal())  # a freshly built identical proposal

    stored = json.loads(rows(path, "SELECT payload_json FROM proposals")[0]["payload_json"])
    assert stored["instrument_type"] == "equity"
    assert stored["quantity"] == 10


def test_a_genuinely_changed_legacy_proposal_still_conflicts(tmp_path) -> None:
    path = tmp_path / "conflict.db"
    seed_v1(path)
    repository = LifecycleRepository(Database(path))

    with pytest.raises(PersistenceConflictError):
        repository.save_proposal(equity_proposal(quantity=11))


def test_broker_order_round_trips_with_the_new_columns(tmp_path) -> None:
    path = tmp_path / "snapshot.db"
    seed_v1(path)
    repository = LifecycleRepository(Database(path))

    snapshot = BrokerOrderSnapshot(
        alpaca_order_id="alpaca-legacy-1",
        client_order_id=LEGACY_ORDER["client_order_id"],
        proposal_id=LEGACY_PROPOSAL_ID,
        symbol="AAPL",
        side="BUY",
        quantity=10,
        lifecycle_status=OrderLifecycleState.FILLED,
        broker_status="filled",
        submitted_at=datetime(2026, 9, 1, 13, tzinfo=timezone.utc),
    )
    repository.save_broker_order(snapshot)

    restored = repository.get_broker_order(LEGACY_ORDER["client_order_id"])
    assert restored is not None
    assert restored.asset_class == "us_equity"
    assert restored.order_class == "simple"
    assert restored.underlying is None
    assert restored.legs is None
    assert rows(path, "SELECT legs_json FROM broker_orders")[0]["legs_json"] is None


def test_snapshots_written_before_options_still_deserialize() -> None:
    """A payload_json row written by schema v1 carries none of the new fields."""

    legacy = (
        '{"alpaca_order_id":"a1","client_order_id":"c1","proposal_id":"p1","symbol":"AAPL",'
        '"side":"BUY","quantity":10.0,"lifecycle_status":"FILLED","broker_status":"filled",'
        '"submitted_at":"2026-09-01T13:00:00+00:00","filled_at":null,"filled_quantity":0.0,'
        '"filled_avg_price":null,"updated_at":"2026-09-01T13:00:06+00:00","paper":true}'
    )
    restored = BrokerOrderSnapshot.model_validate_json(legacy)
    assert restored.asset_class == "us_equity"
    assert restored.order_class == "simple"
    assert restored.legs is None


# --------------------------------------------------------------------------
# The real database, migrated on a copy
# --------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_DATABASE.exists(), reason="no local production database")
def test_real_database_copy_migrates_cleanly(tmp_path) -> None:
    """Migrate a copy of the real database. The original is never opened for write."""

    original_bytes = REAL_DATABASE.read_bytes()
    copy = tmp_path / "real_copy.db"
    shutil.copyfile(REAL_DATABASE, copy)

    before = {
        table: rows(copy, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        for table in (
            "proposals",
            "portfolio_snapshots",
            "market_risk_snapshots",
            "risk_reports",
            "ai_risk_analyses",
            "governor_decisions",
            "execution_authorizations",
            "execution_results",
            "broker_orders",
            "audit_events",
        )
    }

    database = Database(copy)
    repository = LifecycleRepository(database)

    assert version_of(copy) == SCHEMA_VERSION
    after = {table: rows(copy, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in before}
    assert after == before, "no row may be lost or added by the migration"

    proposals = rows(copy, "SELECT proposal_id, payload_json FROM proposals")
    assert proposals, "the real database should contain proposals to exercise"
    for row in proposals:
        assert json.loads(row["payload_json"])["instrument_type"] == "equity"
        stored = repository.get_proposal(row["proposal_id"])
        assert stored is not None
        repository.save_proposal(stored)  # re-evaluation must not conflict

    assert rows(copy, "PRAGMA foreign_key_check") == []
    assert "legs_json" in columns_of(copy, "broker_orders")
    assert REAL_DATABASE.read_bytes() == original_bytes, "the real database must be untouched"
