-- =============================================================================
-- 002_tenant_isolation.sql -- per-tenant schemas, append-only ledgers, hash chain.
-- Idempotent: every statement is IF NOT EXISTS / OR REPLACE / guarded.
--
-- Implements at the DATABASE SCHEMA level (not in application code):
--   A2  append-only, hash-chained: no UPDATE, DELETE or TRUNCATE path on ledger
--       tables at any privilege level short of dropping the trigger, which the
--       application role cannot do because it owns nothing.
--   B3  cross-tenant access impossible by construction: one schema per tenant,
--       one NOLOGIN role per tenant with USAGE on its own schema only.
--   B1  the environment recorded in any ledger row can only be "paper".
--   A5  a tenant can verify linkage + contiguity of its own chain with one
--       SELECT and no Mizan code (content hashes: python -m mizan.audit.verify_chain).
--
-- Naming: tenant id `tenant-a`  ->  schema `tenant_tenant_a`, role `tenant_tenant_a_app`.
--   '-' becomes '_' (tenant ids cannot contain '_', so the mapping is injective).
--   Tenant ids longer than 52 characters are refused: `tenant_<id>_app` must fit
--   PostgreSQL's 63-byte identifier limit without silent truncation.
--
-- Chains: decision_records and control_events each carry their OWN sequence /
-- audit_prev_hash chain for now. Sprint 3 merges them into one per-tenant chain
-- (API-SURFACE Addendum 1 §B.6: "prev = last record of any type").
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- Trigger function: append-only. Statement-level, so it fires even when no row
-- matches (UPDATE ... WHERE false is refused too) and because TRUNCATE triggers
-- must be statement-level.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mizan_admin.mizan_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
  RAISE EXCEPTION 'append-only: % rows cannot be modified or deleted', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation',
          HINT = 'Mizan ledgers are append-only (Hard Rule A2). Append a new record instead.';
  RETURN NULL;
END
$fn$;
ALTER FUNCTION mizan_admin.mizan_append_only() OWNER TO mizan_admin;

-- -----------------------------------------------------------------------------
-- Trigger function: chain linking. BEFORE INSERT FOR EACH ROW on a chained table.
-- Requires sequence == max(sequence) + 1 and audit_prev_hash == audit_hash of the
-- current tail (64 zeros when the table is empty). Appenders are serialised per
-- table with a transaction-scoped advisory lock so two concurrent inserts cannot
-- both read the same tail; the primary key is the second line of defence.
-- SECURITY DEFINER (owner mizan_admin) so the tail read does not depend on the
-- caller's privileges; identifiers come from the trigger context, never from input.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mizan_admin.mizan_chain_link()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  last_sequence bigint;
  last_hash     text;
  zero_hash     constant text := repeat('0', 64);
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME));

  EXECUTE format('SELECT sequence, audit_hash::text FROM %I.%I ORDER BY sequence DESC LIMIT 1',
                 TG_TABLE_SCHEMA, TG_TABLE_NAME)
    INTO last_sequence, last_hash;

  IF last_sequence IS NULL THEN
    last_sequence := 0;
    last_hash := zero_hash;
  END IF;

  IF NEW.sequence <> last_sequence + 1 THEN
    RAISE EXCEPTION 'chain: %.% sequence must be % (got %)',
      TG_TABLE_SCHEMA, TG_TABLE_NAME, last_sequence + 1, NEW.sequence
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF NEW.audit_prev_hash::text <> last_hash THEN
    RAISE EXCEPTION 'chain: %.% audit_prev_hash must equal the previous audit_hash % (got %)',
      TG_TABLE_SCHEMA, TG_TABLE_NAME, last_hash, NEW.audit_prev_hash
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  RETURN NEW;
END
$fn$;
ALTER FUNCTION mizan_admin.mizan_chain_link() OWNER TO mizan_admin;

-- -----------------------------------------------------------------------------
-- Chain report over one chained table. SECURITY INVOKER: the caller needs SELECT
-- on the table. Verifies contiguity and prev-hash linkage; the content hash of
-- each record is verified offline by `python -m mizan.audit.verify_chain`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mizan_admin.chain_report(schema_name text, table_name text DEFAULT 'decision_records')
RETURNS TABLE (ok boolean, chain_length bigint, first_bad_sequence bigint, detail text)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  problem text;
BEGIN
  IF table_name NOT IN ('decision_records', 'control_events') THEN
    RAISE EXCEPTION 'chain_report: % is not a chained table', table_name
      USING ERRCODE = 'invalid_parameter_value';
  END IF;

  EXECUTE format($q$
    WITH ordered AS (
      SELECT sequence,
             audit_prev_hash::text AS prev,
             lag(sequence)          OVER (ORDER BY sequence) AS prev_sequence,
             lag(audit_hash::text)  OVER (ORDER BY sequence) AS prev_hash
      FROM %I.%I
    ),
    judged AS (
      SELECT sequence,
             CASE
               WHEN prev_sequence IS NULL AND sequence <> 1 THEN 'first sequence is not 1'
               WHEN prev_sequence IS NULL AND prev <> repeat('0', 64) THEN 'first record does not link to the zero hash'
               WHEN prev_sequence IS NOT NULL AND sequence <> prev_sequence + 1 THEN 'sequence gap'
               WHEN prev_sequence IS NOT NULL AND prev <> prev_hash THEN 'audit_prev_hash does not match the previous audit_hash'
             END AS problem
      FROM ordered
    )
    SELECT (SELECT count(*) FROM ordered),
           (SELECT min(sequence) FROM judged WHERE problem IS NOT NULL),
           (SELECT judged.problem FROM judged WHERE problem IS NOT NULL ORDER BY sequence LIMIT 1)
  $q$, schema_name, table_name)
  INTO chain_length, first_bad_sequence, problem;

  ok := first_bad_sequence IS NULL;
  detail := coalesce(
    problem,
    format('linkage and contiguity verified for %s record(s); verify content hashes with python -m mizan.audit.verify_chain',
           chain_length));
  RETURN NEXT;
END
$fn$;
ALTER FUNCTION mizan_admin.chain_report(text, text) OWNER TO mizan_admin;
REVOKE EXECUTE ON FUNCTION mizan_admin.chain_report(text, text) FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- Provisioning: mizan_admin.create_tenant(tenant_id)
-- SECURITY DEFINER, owned by mizan_admin (NOT a superuser). Validates the id,
-- creates the schema, the NOLOGIN app role, the tables, the triggers and the
-- grants. Safe to call again for an existing tenant.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mizan_admin.create_tenant(tenant_id text)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  schema_name text;
  app_role    text;
  tbl         text;
BEGIN
  IF tenant_id IS NULL OR tenant_id !~ '^[a-z0-9][a-z0-9-]{0,62}$' THEN
    RAISE EXCEPTION 'invalid tenant_id %: must match ^[a-z0-9][a-z0-9-]{0,62}$', coalesce(tenant_id, '<null>')
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF length(tenant_id) > 52 THEN
    RAISE EXCEPTION 'tenant_id % is longer than 52 characters: tenant_<id>_app must fit a 63-byte identifier', tenant_id
      USING ERRCODE = 'invalid_parameter_value';
  END IF;

  schema_name := 'tenant_' || replace(tenant_id, '-', '_');
  app_role    := schema_name || '_app';

  -- Schema owned by mizan_admin, never by the app role.
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION mizan_admin', schema_name);
  EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', schema_name);

  -- Per-tenant NOLOGIN role. The operator grants it to a LOGIN role.
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = app_role) THEN
    EXECUTE format(
      'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
      app_role);
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), app_role);
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', schema_name, app_role);

  -- ---- decision_records: the hash-chained ledger (DecisionRecord contract) ----
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS %I.decision_records (
      sequence        bigint      PRIMARY KEY CHECK (sequence >= 1),
      decision_id     text        NOT NULL UNIQUE,
      audit_prev_hash char(64)    NOT NULL CHECK (audit_prev_hash ~ '^[0-9a-f]{64}$'),
      audit_hash      char(64)    NOT NULL UNIQUE CHECK (audit_hash ~ '^[0-9a-f]{64}$'),
      tenant_id       text        NOT NULL CHECK (tenant_id = %L),
      record          jsonb       NOT NULL CHECK (jsonb_typeof(record) = 'object'),
      recorded_at     timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT decision_records_genesis     CHECK (sequence > 1 OR audit_prev_hash = repeat('0', 64)),
      CONSTRAINT decision_records_record_id   CHECK (record->>'decision_id' IS NOT DISTINCT FROM decision_id),
      CONSTRAINT decision_records_record_seq  CHECK ((record->>'sequence')::bigint IS NOT DISTINCT FROM sequence),
      CONSTRAINT decision_records_record_hash CHECK (record->>'audit_hash' IS NOT DISTINCT FROM audit_hash::text),
      CONSTRAINT decision_records_record_prev CHECK (record->>'audit_prev_hash' IS NOT DISTINCT FROM audit_prev_hash::text),
      CONSTRAINT decision_records_record_tnt  CHECK (record->>'tenant_id' IS NOT DISTINCT FROM tenant_id),
      CONSTRAINT decision_records_paper_only  CHECK (record #>> '{execution,environment}' IS NULL
                                                     OR record #>> '{execution,environment}' = 'paper')
    )$sql$, schema_name, tenant_id);

  -- ---- control_events: graduated-response / kill-switch chain (ControlEvent contract) ----
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS %I.control_events (
      sequence        bigint      PRIMARY KEY CHECK (sequence >= 1),
      event_id        text        NOT NULL UNIQUE,
      event_type      text        NOT NULL CHECK (event_type IN
                        ('response_level_changed', 'kill_switch_activated', 'kill_switch_deactivated', 'policy_activated')),
      from_level      smallint    CHECK (from_level BETWEEN 0 AND 5),
      to_level        smallint    CHECK (to_level BETWEEN 0 AND 5),
      actor_type      text        NOT NULL CHECK (actor_type IN ('system', 'human')),
      actor_id        text        NOT NULL,
      audit_prev_hash char(64)    NOT NULL CHECK (audit_prev_hash ~ '^[0-9a-f]{64}$'),
      audit_hash      char(64)    NOT NULL UNIQUE CHECK (audit_hash ~ '^[0-9a-f]{64}$'),
      tenant_id       text        NOT NULL CHECK (tenant_id = %L),
      record          jsonb       NOT NULL CHECK (jsonb_typeof(record) = 'object'),
      occurred_at     timestamptz NOT NULL,
      recorded_at     timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT control_events_genesis      CHECK (sequence > 1 OR audit_prev_hash = repeat('0', 64)),
      CONSTRAINT control_events_levels       CHECK (event_type <> 'response_level_changed'
                                                    OR (from_level IS NOT NULL AND to_level IS NOT NULL)),
      -- R-GRAD-1: automatic escalation is one-directional; de-escalation needs a human.
      CONSTRAINT control_events_human_deesc  CHECK (from_level IS NULL OR to_level IS NULL
                                                    OR to_level >= from_level OR actor_type = 'human'),
      CONSTRAINT control_events_record_id    CHECK (record->>'event_id' IS NOT DISTINCT FROM event_id),
      CONSTRAINT control_events_record_seq   CHECK ((record->>'sequence')::bigint IS NOT DISTINCT FROM sequence),
      CONSTRAINT control_events_record_hash  CHECK (record->>'audit_hash' IS NOT DISTINCT FROM audit_hash::text),
      CONSTRAINT control_events_record_prev  CHECK (record->>'audit_prev_hash' IS NOT DISTINCT FROM audit_prev_hash::text),
      CONSTRAINT control_events_record_tnt   CHECK (record->>'tenant_id' IS NOT DISTINCT FROM tenant_id)
    )$sql$, schema_name, tenant_id);

  -- ---- policies: versioned, hash-signed policy documents ----
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS %I.policies (
      policy_id      text        NOT NULL CHECK (policy_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
      policy_version text        NOT NULL,
      policy_hash    char(64)    NOT NULL UNIQUE CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
      tenant_id      text        NOT NULL CHECK (tenant_id = %L),
      document       jsonb       NOT NULL CHECK (jsonb_typeof(document) = 'object'),
      created_at     timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (policy_id, policy_version),
      CONSTRAINT policies_document_hash CHECK (document->>'policy_hash' IS NOT DISTINCT FROM policy_hash::text),
      CONSTRAINT policies_document_tnt  CHECK (document->>'tenant_id' IS NOT DISTINCT FROM tenant_id)
    )$sql$, schema_name, tenant_id);

  -- ---- authorizations: short-lived, state-bound execution authorizations ----
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS %I.authorizations (
      authorization_id   text        PRIMARY KEY,
      decision_id        text        NOT NULL,
      authorization_hash char(64)    NOT NULL UNIQUE CHECK (authorization_hash ~ '^[0-9a-f]{64}$'),
      tenant_id          text        NOT NULL CHECK (tenant_id = %L),
      issued_at          timestamptz NOT NULL,
      expires_at         timestamptz NOT NULL CHECK (expires_at > issued_at),
      document           jsonb       NOT NULL CHECK (jsonb_typeof(document) = 'object'),
      recorded_at        timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT authorizations_paper_only CHECK (NOT (document ? 'environment') OR document->>'environment' = 'paper')
    )$sql$, schema_name, tenant_id);
  EXECUTE format('CREATE INDEX IF NOT EXISTS authorizations_decision_id_idx ON %I.authorizations (decision_id)',
                 schema_name);

  -- ---- execution_results: append-only event log; a state change is a NEW row ----
  EXECUTE format($sql$
    CREATE TABLE IF NOT EXISTS %I.execution_results (
      result_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      decision_id      text        NOT NULL,
      authorization_id text,
      status           text        NOT NULL,
      client_order_id  text,
      broker_order_id  text,
      tenant_id        text        NOT NULL CHECK (tenant_id = %L),
      document         jsonb       NOT NULL CHECK (jsonb_typeof(document) = 'object'),
      recorded_at      timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT execution_results_paper_only CHECK (NOT (document ? 'environment') OR document->>'environment' = 'paper')
    )$sql$, schema_name, tenant_id);
  EXECUTE format('CREATE INDEX IF NOT EXISTS execution_results_decision_id_idx ON %I.execution_results (decision_id)',
                 schema_name);

  -- ---- append-only triggers: BEFORE UPDATE OR DELETE OR TRUNCATE, every ledger table ----
  FOREACH tbl IN ARRAY ARRAY['decision_records', 'control_events', 'policies', 'authorizations', 'execution_results']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER %I BEFORE UPDATE OR DELETE OR TRUNCATE ON %I.%I '
      'FOR EACH STATEMENT EXECUTE FUNCTION mizan_admin.mizan_append_only()',
      tbl || '_append_only', schema_name, tbl);
  END LOOP;

  -- ---- chain-linking triggers: BEFORE INSERT on the two chained tables ----
  FOREACH tbl IN ARRAY ARRAY['decision_records', 'control_events']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER %I BEFORE INSERT ON %I.%I '
      'FOR EACH ROW EXECUTE FUNCTION mizan_admin.mizan_chain_link()',
      tbl || '_chain_link', schema_name, tbl);
  END LOOP;

  -- ---- per-tenant chain verification, callable by the tenant's own app role ----
  -- SECURITY DEFINER so it may call mizan_admin.chain_report; the schema is fixed
  -- at creation time, so it can only ever report on this tenant's tables.
  EXECUTE format($sql$
    CREATE OR REPLACE FUNCTION %I.verify_chain(table_name text DEFAULT 'decision_records')
    RETURNS TABLE (ok boolean, chain_length bigint, first_bad_sequence bigint, detail text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
    AS $body$ SELECT * FROM mizan_admin.chain_report(%L, table_name) $body$
  $sql$, schema_name, schema_name);
  EXECUTE format('ALTER FUNCTION %I.verify_chain(text) OWNER TO mizan_admin', schema_name);
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %I.verify_chain(text) FROM PUBLIC', schema_name);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %I.verify_chain(text) TO %I', schema_name, app_role);

  -- ---- grants: SELECT + INSERT only; UPDATE, DELETE, TRUNCATE explicitly revoked ----
  EXECUTE format('GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA %I TO %I', schema_name, app_role);
  EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA %I FROM %I',
                 schema_name, app_role);
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', schema_name, app_role);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE mizan_admin IN SCHEMA %I GRANT SELECT, INSERT ON TABLES TO %I',
                 schema_name, app_role);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE mizan_admin IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I',
                 schema_name, app_role);

  INSERT INTO mizan_admin.tenants (tenant_id, schema_name, app_role)
  VALUES (tenant_id, schema_name, app_role)
  ON CONFLICT (tenant_id) DO NOTHING;

  RETURN schema_name;
END
$fn$;
ALTER FUNCTION mizan_admin.create_tenant(text) OWNER TO mizan_admin;
REVOKE EXECUTE ON FUNCTION mizan_admin.create_tenant(text) FROM PUBLIC;
