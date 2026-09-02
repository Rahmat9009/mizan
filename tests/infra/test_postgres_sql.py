"""infra/postgres/init/*.sql -- Hard Rule A2 and B3 enforced in the database, not in code.

These are STATIC assertions. The Docker daemon is unavailable on the development box, so
nothing here executes SQL; the CI `postgres-ddl` job applies the same files to a real
postgres:16 service container, applies them a second time to prove idempotency, and runs
`infra/postgres/verify/prove_append_only.sh`, which tries every forbidden statement as
both the superuser and the tenant application role and requires each to be refused.

What a static test can still establish, and what makes it worth writing: A2 says append-only
is enforced *at the database schema level*. That claim is falsifiable by reading the DDL --
either the triggers exist on both chained tables for UPDATE, DELETE and TRUNCATE, and the
application role is granted no privilege that could bypass them, or the claim is false. A
deleted trigger or a widened GRANT is exactly the kind of change that passes review by
looking small, and it fails here.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_DIR = REPO_ROOT / "infra" / "postgres" / "init"

# docs/API-SURFACE.md §2.1: TenantId. The database must not accept an id the contracts reject.
TENANT_ID_PATTERN = "^[a-z0-9][a-z0-9-]{0,62}$"
CHAINED_TABLES = ("decision_records", "control_events")
LEDGER_TABLES = CHAINED_TABLES + ("policies", "authorizations", "execution_results")
# Everything the per-tenant application role may ever hold. UPDATE, DELETE, TRUNCATE,
# REFERENCES and TRIGGER are absent on purpose; so is ALL.
ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT", "USAGE", "CONNECT", "EXECUTE"})


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` comments, leaving string literals intact.

    Single-quote state is tracked so an apostrophe inside a comment cannot open a string
    and a ``--`` inside a literal cannot start a comment. ``''`` closes and immediately
    reopens, which is the correct behaviour for an escaped quote.
    """
    out: list[str] = []
    index, length, in_string = 0, len(sql), False
    while index < length:
        char = sql[index]
        if in_string:
            out.append(char)
            in_string = char != "'"
            index += 1
        elif char == "'":
            in_string = True
            out.append(char)
            index += 1
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
        else:
            out.append(char)
            index += 1
    return "".join(out)


SQL_FILES = sorted(INIT_DIR.glob("*.sql"))
RAW = {path.name: path.read_text(encoding="utf-8") for path in SQL_FILES}
CODE = {name: _strip_sql_comments(text) for name, text in RAW.items()}
ALL_CODE = "\n".join(CODE[name] for name in sorted(CODE))


def _load_scanner():
    path = REPO_ROOT / "scripts" / "secret_scan.py"
    spec = importlib.util.spec_from_file_location("mizan_secret_scan_for_sql", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


SCAN = _load_scanner()


def test_the_init_directory_has_files_and_they_are_ordered() -> None:
    assert SQL_FILES, "no init scripts; the compose mount would be empty"
    assert [path.name for path in SQL_FILES] == sorted(RAW), "psql applies them in lexical order"
    assert SQL_FILES[0].name.startswith("001"), "roles must be created before anything is granted to them"


# ---------------------------------------------------------------------------
# tenant id: the only untrusted input any of this DDL takes
# ---------------------------------------------------------------------------
def test_the_tenant_id_regex_is_present_and_anchored() -> None:
    occurrences = re.findall(r"'(\^[^']*\$)'", ALL_CODE)
    tenant_patterns = [pattern for pattern in occurrences if pattern == TENANT_ID_PATTERN]
    assert tenant_patterns, f"{TENANT_ID_PATTERN!r} appears nowhere in the DDL"
    for pattern in tenant_patterns:
        assert pattern.startswith("^") and pattern.endswith("$"), (
            f"{pattern!r} is unanchored: an unanchored match accepts 'a; DROP SCHEMA public'"
        )


def test_the_tenant_registry_constrains_the_id_as_well_as_the_function() -> None:
    """Belt and braces: create_tenant() validates, and the table CHECK re-validates."""
    assert f"CHECK (tenant_id ~ '{TENANT_ID_PATTERN}')" in CODE["001_roles.sql"]
    assert f"tenant_id !~ '{TENANT_ID_PATTERN}'" in CODE["002_tenant_isolation.sql"]


@pytest.mark.parametrize("tenant_id", ["a", "tenant-a", "t0", "a" * 63, "0-9-a"])
def test_the_regex_accepts_a_valid_tenant_id(tenant_id: str) -> None:
    assert re.fullmatch(TENANT_ID_PATTERN, tenant_id)


@pytest.mark.parametrize(
    "tenant_id",
    ["", "Tenant", "tenant_a", "-leading", "a" * 64, "x; DROP SCHEMA public", "tenant a",
     'a" OR "1"="1', "tenant-a\nDROP SCHEMA public", "../etc"],
)  # fmt: skip
def test_the_regex_rejects_an_invalid_tenant_id(tenant_id: str) -> None:
    assert re.fullmatch(TENANT_ID_PATTERN, tenant_id) is None


def test_identifiers_are_interpolated_with_format_not_string_concatenation() -> None:
    """%I quotes an identifier; `||` into a DDL string is how injection gets in."""
    body = CODE["002_tenant_isolation.sql"]
    assert "EXECUTE format(" in body
    assert not re.search(r"EXECUTE\s+'[^']*'\s*\|\|", body), "an EXECUTE built by concatenation"


def test_the_provisioning_function_pins_its_search_path() -> None:
    """A SECURITY DEFINER function without a fixed search_path is a privilege-escalation hole."""
    definers = re.findall(r"SECURITY DEFINER(.{0,120})", ALL_CODE, re.S)
    assert definers, "no SECURITY DEFINER function found"
    for tail in definers:
        assert "SET search_path" in tail, f"SECURITY DEFINER without SET search_path near: {tail[:60]!r}"


# ---------------------------------------------------------------------------
# A2: append-only, enforced by the schema
# ---------------------------------------------------------------------------
def _append_only_tables() -> list[str]:
    match = re.search(
        r"FOREACH\s+\w+\s+IN\s+ARRAY\s+ARRAY\[(?P<tables>.*?)\]\s*LOOP(?P<body>.*?)END LOOP",
        CODE["002_tenant_isolation.sql"],
        re.S,
    )
    assert match, "no append-only trigger loop found"
    assert "mizan_append_only" in match.group("body")
    return re.findall(r"'([a-z_]+)'", match.group("tables"))


def _append_only_trigger_ddl() -> str:
    match = re.search(
        r"FOREACH.*?mizan_admin\.mizan_append_only\(\)", CODE["002_tenant_isolation.sql"], re.S
    )
    assert match
    return re.sub(r"'\s*\n\s*'", "", match.group(0))  # rejoin the split SQL string literal


@pytest.mark.parametrize("table", CHAINED_TABLES)
def test_the_chained_table_has_an_append_only_trigger(table: str) -> None:
    assert table in _append_only_tables(), f"{table} has no append-only trigger"


def test_the_ledger_tables_all_get_the_trigger_not_only_the_chained_ones() -> None:
    tables = set(_append_only_tables())
    assert {"policies", "authorizations", "execution_results"} <= tables, (
        "a policy or an authorization that can be edited after the fact is as damaging as an "
        "editable decision record"
    )


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_the_trigger_fires_before_the_operation(operation: str) -> None:
    ddl = _append_only_trigger_ddl()
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in ddl, ddl
    assert operation in ddl
    assert "FOR EACH STATEMENT" in ddl, (
        "row-level triggers do not fire for TRUNCATE, and never fire for an UPDATE that "
        "matches no rows -- both must be refused"
    )


def test_the_append_only_trigger_function_raises() -> None:
    body = re.search(
        r"CREATE OR REPLACE FUNCTION mizan_admin\.mizan_append_only\(\).*?\$fn\$(.*?)\$fn\$",
        CODE["002_tenant_isolation.sql"],
        re.S,
    )
    assert body, "mizan_append_only() is not defined"
    assert "RAISE EXCEPTION" in body.group(1)
    assert "ERRCODE" in body.group(1), "a raise without an errcode is hard to handle upstream"
    assert "RETURN NULL" in body.group(1)


def test_there_is_no_update_or_delete_statement_anywhere_in_the_ddl() -> None:
    for name, code in CODE.items():
        assert not re.search(r"^\s*UPDATE\s+\w", code, re.M | re.I), f"{name} contains an UPDATE"
        assert not re.search(r"\bDELETE\s+FROM\b", code, re.I), f"{name} contains a DELETE"
        assert not re.search(r"\bDROP\s+(TABLE|SCHEMA|TRIGGER|ROLE)\b", code, re.I), f"{name} drops an object"


# ---------------------------------------------------------------------------
# the hash chain, enforced on INSERT
# ---------------------------------------------------------------------------
def _chain_link_ddl() -> str:
    match = re.search(
        r"FOREACH.*?mizan_admin\.mizan_chain_link\(\)", CODE["002_tenant_isolation.sql"], re.S
    )
    assert match, "no chain-linking trigger loop found"
    return re.sub(r"'\s*\n\s*'", "", match.group(0))


def test_the_chain_link_trigger_is_before_insert_on_both_chained_tables() -> None:
    ddl = _chain_link_ddl()
    assert "BEFORE INSERT" in ddl
    assert "FOR EACH ROW" in ddl, "the link is a property of each record, not of a statement"
    tables = re.findall(r"'([a-z_]+)'", ddl.split("LOOP", 1)[0])
    assert set(CHAINED_TABLES) <= set(tables), tables


def _chain_link_body() -> str:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION mizan_admin\.mizan_chain_link\(\).*?\$fn\$(.*?)\$fn\$",
        CODE["002_tenant_isolation.sql"],
        re.S,
    )
    assert match, "mizan_chain_link() is not defined"
    return match.group(1)


def test_the_chain_link_trigger_checks_the_sequence_against_the_maximum_plus_one() -> None:
    body = _chain_link_body()
    assert "ORDER BY sequence DESC LIMIT 1" in body, "the tail of the chain is never read"
    assert re.search(r"NEW\.sequence\s*<>\s*last_sequence\s*\+\s*1", body), (
        "CANONICAL.md §5 rule 2: a deletion is detected by the gap it leaves in the sequence"
    )
    assert "RAISE EXCEPTION" in body


def test_the_chain_link_trigger_checks_the_previous_hash_against_the_tail() -> None:
    body = _chain_link_body()
    assert re.search(r"NEW\.audit_prev_hash::text\s*<>\s*last_hash", body), (
        "CANONICAL.md §5 rule 3: reordering and insertion are detected by the broken link"
    )
    assert "repeat('0', 64)" in body, "the genesis record must link to the ZERO_HASH"


def test_concurrent_appenders_cannot_both_read_the_same_tail() -> None:
    body = _chain_link_body()
    assert "pg_advisory_xact_lock" in body, "two concurrent inserts would both see the same tail"


def _table_body(table: str) -> str:
    definition = re.search(
        rf"CREATE TABLE IF NOT EXISTS %I\.{table} \((.*?)\)\$sql\$", CODE["002_tenant_isolation.sql"], re.S
    )
    assert definition, f"{table} is not created in create_tenant()"
    return definition.group(1)


@pytest.mark.parametrize("table", CHAINED_TABLES)
def test_the_chained_table_constrains_its_own_genesis_and_hash_columns(table: str) -> None:
    body = _table_body(table)
    assert "audit_prev_hash char(64)" in body and "audit_hash      char(64)" in body
    assert body.count("'^[0-9a-f]{64}$'") >= 2, "the hash columns are not constrained to lowercase hex"
    assert "audit_hash      char(64)    NOT NULL UNIQUE" in body, "a repeated audit_hash would fork the chain"
    assert re.search(rf"CONSTRAINT {table}_genesis\s+CHECK \(sequence > 1 OR audit_prev_hash = repeat", body)
    assert "CHECK (sequence >= 1)" in body


# ---------------------------------------------------------------------------
# B3: the application role can append and read, and nothing else
# ---------------------------------------------------------------------------
def _privileges(statement: str) -> set[str]:
    return {token.strip().upper() for token in statement.split(",") if token.strip()}


def test_no_grant_ever_exceeds_select_insert_usage() -> None:
    for privileges, tail in re.findall(r"\bGRANT\s+(.+?)\s+ON\b(.{0,80})", ALL_CODE, re.S):
        granted = _privileges(privileges)
        assert granted <= ALLOWED_PRIVILEGES, (
            f"GRANT {privileges} ON{tail[:50]!r} exceeds {sorted(ALLOWED_PRIVILEGES)}"
        )


def test_there_is_no_grant_all() -> None:
    assert not re.search(r"\bGRANT\s+ALL\b", ALL_CODE, re.I), (
        "GRANT ALL includes UPDATE, DELETE and TRUNCATE, which is the whole thing this schema forbids"
    )


def test_the_app_role_is_explicitly_revoked_the_mutating_privileges() -> None:
    """The GRANT above never gave them. The REVOKE says so anyway, and survives a future GRANT."""
    revokes = re.findall(r"\bREVOKE\s+(.+?)\s+ON ALL TABLES IN SCHEMA", ALL_CODE, re.S)
    assert revokes, "no blanket REVOKE on the tenant's tables"
    revoked = set().union(*(_privileges(statement) for statement in revokes))
    assert {"UPDATE", "DELETE", "TRUNCATE"} <= revoked, sorted(revoked)
    assert {"REFERENCES", "TRIGGER"} <= revoked, "TRIGGER would let the role disable the append-only trigger"


def test_the_public_schema_is_revoked_from_public() -> None:
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in CODE["001_roles.sql"]
    assert "REVOKE ALL ON DATABASE" in CODE["001_roles.sql"]
    assert "REVOKE ALL ON SCHEMA mizan_admin FROM PUBLIC" in CODE["001_roles.sql"]


def test_every_tenant_schema_is_revoked_from_public_too() -> None:
    assert "REVOKE ALL ON SCHEMA %I FROM PUBLIC" in CODE["002_tenant_isolation.sql"]


def test_the_provisioning_function_is_not_executable_by_public() -> None:
    for function in ("create_tenant(text)", "chain_report(text, text)"):
        assert f"REVOKE EXECUTE ON FUNCTION mizan_admin.{function} FROM PUBLIC" in ALL_CODE


def test_no_role_created_here_can_log_in_or_is_a_superuser() -> None:
    for match in re.finditer(r"CREATE ROLE\s+(?:%I|\w+)(.{0,140})", ALL_CODE, re.S):
        attributes = match.group(1).upper()
        assert "NOLOGIN" in attributes, "a NOLOGIN role cannot be used to connect if its grants leak"
        assert "NOSUPERUSER" in attributes
        assert "NOBYPASSRLS" in attributes
        assert not re.search(r"\bSUPERUSER\b(?<!NOSUPERUSER)", attributes)


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_each_tenant_table_is_pinned_to_its_own_tenant_id(table: str) -> None:
    """B3 is separate schemas; the CHECK is the second line, so a row cannot claim another tenant."""
    body = _table_body(table)
    assert re.search(r"tenant_id\s+text\s+NOT NULL CHECK \(tenant_id = %L\)", body), (
        f"{table} does not pin tenant_id to the schema's own tenant"
    )


# ---------------------------------------------------------------------------
# B1: no environment but paper can be stored
# ---------------------------------------------------------------------------
def test_the_word_live_appears_in_no_check_constraint() -> None:
    assert "'live'" not in ALL_CODE
    paper_checks = re.findall(r"paper_only\s+CHECK \((.*?)\)\n", CODE["002_tenant_isolation.sql"], re.S)
    assert len(paper_checks) >= 3, "decision_records, authorizations and execution_results each need one"
    for check in paper_checks:
        assert "'paper'" in check


# ---------------------------------------------------------------------------
# no credential, anywhere
# ---------------------------------------------------------------------------
def test_no_password_literal_appears_in_the_ddl() -> None:
    assert not re.search(r"\bPASSWORD\b", ALL_CODE, re.I), (
        "roles are created NOLOGIN and passwordless; the operator sets a password out of band"
    )


def test_the_secret_scanner_finds_nothing_in_the_ddl() -> None:
    for name, text in RAW.items():
        findings = SCAN.scan_text(text, f"infra/postgres/init/{name}")
        assert findings == [], [f.format() for f in findings]


# ---------------------------------------------------------------------------
# idempotency -- the files run on every container start and on every CI run, twice
# ---------------------------------------------------------------------------
GUARDED = {"SCHEMA", "TABLE", "INDEX", "SEQUENCE", "VIEW"}
REPLACEABLE = {"FUNCTION", "TRIGGER", "PROCEDURE", "VIEW", "RULE"}


@pytest.mark.parametrize("name", sorted(RAW))
def test_every_create_statement_is_idempotent(name: str) -> None:
    code = CODE[name]
    for match in re.finditer(r"\bCREATE\b", code):
        window = " ".join(code[match.start() : match.start() + 80].split())
        keyword = re.match(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", window, re.I
        )
        assert keyword, window
        obj = keyword.group(1).upper()
        if obj == "ROLE":
            continue  # PostgreSQL has no CREATE ROLE IF NOT EXISTS; guarded below
        if obj in REPLACEABLE and window.upper().startswith("CREATE OR REPLACE"):
            continue
        if obj in GUARDED and "IF NOT EXISTS" in window.upper():
            continue
        pytest.fail(f"{name}: not idempotent -> {window[:70]!r}")


def test_every_create_role_is_guarded_by_an_existence_check() -> None:
    for name, code in CODE.items():
        creates = len(re.findall(r"\bCREATE ROLE\b", code))
        guards = len(re.findall(r"IF NOT EXISTS \(SELECT 1 FROM pg_catalog\.pg_roles WHERE rolname", code))
        assert guards >= creates, f"{name}: {creates} CREATE ROLE, {guards} existence guard(s)"


def test_the_tenant_registry_insert_tolerates_a_re_run() -> None:
    code = CODE["002_tenant_isolation.sql"]
    inserts = re.findall(r"INSERT INTO mizan_admin\.tenants(.*?);", code, re.S)
    assert inserts, "create_tenant() does not register the tenant"
    for statement in inserts:
        assert "ON CONFLICT (tenant_id) DO NOTHING" in statement


def test_every_file_stops_on_the_first_error() -> None:
    for name, text in RAW.items():
        assert "\\set ON_ERROR_STOP on" in text, f"{name} would continue past a failed statement"
