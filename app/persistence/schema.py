from __future__ import annotations


SCHEMA_VERSION = 2

# The lowest version this code knows how to migrate from. A database stamped
# below this cannot be upgraded by the current migration set.
MINIMUM_MIGRATABLE_VERSION = 1

# `broker_orders` carries both equity SIMPLE orders and, from v2 onward, option
# SIMPLE and MLEG orders. A multi-leg parent order has no single symbol or side
# of its own, so both columns are nullable; the leg structure lives in
# `legs_json` beside the identity columns reconciliation queries by.
#
# This DDL is shared by fresh initialization and by the v2 table rebuild, so the
# two paths cannot drift apart.
BROKER_ORDERS_COLUMNS = """
    client_order_id TEXT PRIMARY KEY,
    alpaca_order_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE RESTRICT,
    symbol TEXT,
    side TEXT,
    quantity REAL NOT NULL CHECK(quantity > 0),
    lifecycle_status TEXT NOT NULL,
    broker_status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    filled_at TEXT,
    filled_quantity REAL,
    filled_avg_price REAL,
    updated_at TEXT NOT NULL,
    paper INTEGER NOT NULL CHECK(paper = 1),
    asset_class TEXT NOT NULL DEFAULT 'us_equity',
    order_class TEXT NOT NULL DEFAULT 'simple',
    underlying TEXT,
    legs_json TEXT,
    payload_json TEXT NOT NULL
"""

# Columns present in schema v1, in a fixed order. The v2 rebuild copies exactly
# these and lets the new columns take their defaults; no value is invented for
# a row written before options support existed.
BROKER_ORDERS_V1_COLUMNS = (
    "client_order_id",
    "alpaca_order_id",
    "proposal_id",
    "symbol",
    "side",
    "quantity",
    "lifecycle_status",
    "broker_status",
    "submitted_at",
    "filled_at",
    "filled_quantity",
    "filled_avg_price",
    "updated_at",
    "paper",
    "payload_json",
)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        proposal_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_risk_snapshots (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_reports (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_risk_analyses (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS governor_decisions (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        decided_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_authorizations (
        authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(proposal_id, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_results (
        proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        client_order_id TEXT,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    f"CREATE TABLE IF NOT EXISTS broker_orders ({BROKER_ORDERS_COLUMNS})",
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_audit_proposal_order ON audit_events(proposal_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_orders_proposal ON broker_orders(proposal_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_underlying ON broker_orders(underlying)",
    "CREATE INDEX IF NOT EXISTS idx_results_client_order ON execution_results(client_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_authorizations_proposal ON execution_authorizations(proposal_id, created_at)",
)
