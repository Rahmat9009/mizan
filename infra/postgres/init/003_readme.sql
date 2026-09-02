-- =============================================================================
-- 003_readme.sql -- documentation of the layout, as catalog comments. Idempotent.
--
-- What the two files before this one built:
--
--   mizan_admin (role, NOLOGIN, CREATEROLE, owns the database)
--   mizan_admin (schema)
--     tenants                          registry of provisioned tenants
--     create_tenant(text)              SECURITY DEFINER provisioning function
--     mizan_append_only()              trigger fn: refuses UPDATE / DELETE / TRUNCATE
--     mizan_chain_link()               trigger fn: sequence == max+1, prev hash == tail hash
--     chain_report(schema, table)      linkage + contiguity report
--
--   tenant_<id> (schema, owned by mizan_admin)     one per tenant
--     decision_records                 hash-chained DecisionRecord ledger
--     control_events                   hash-chained ControlEvent ledger (own chain until Sprint 3)
--     policies                         versioned, hash-signed policies (append-only)
--     authorizations                   short-lived state-bound authorizations (append-only)
--     execution_results                append-only event log; a state change is a new row
--     verify_chain(table)              tenant-callable chain report on its own tables
--   tenant_<id>_app (role, NOLOGIN)   USAGE on its schema only; SELECT + INSERT on its tables
--
-- How to use it:
--   SELECT mizan_admin.create_tenant('tenant-a');         -- as a member of mizan_admin
--   CREATE ROLE tenant_a_login LOGIN IN ROLE tenant_tenant_a_app;   -- then \password tenant_a_login
--
-- How to prove the guarantees with psql alone: infra/postgres/README.md and
-- infra/postgres/verify/prove_append_only.sh (the same script CI runs).
--
-- What is deliberately NOT here:
--   * no application login role and no password of any kind;
--   * no `live` value anywhere: the paper-only CHECKs reject anything else;
--   * no superuser use by the application, ever.
-- =============================================================================

\set ON_ERROR_STOP on

COMMENT ON SCHEMA mizan_admin IS
  'Mizan administrative objects. Tenant application roles have no USAGE here.';

COMMENT ON TABLE mizan_admin.tenants IS
  'Registry of provisioned tenants: tenant_id -> schema_name, app_role. Admin-only.';

COMMENT ON FUNCTION mizan_admin.create_tenant(text) IS
  'Provision (idempotently) schema tenant_<id>, role tenant_<id>_app, the append-only ledger tables, '
  'the chain-linking triggers and the grants. tenant_id must match ^[a-z0-9][a-z0-9-]{0,62}$ and be <= 52 chars.';

COMMENT ON FUNCTION mizan_admin.mizan_append_only() IS
  'Statement-level BEFORE UPDATE OR DELETE OR TRUNCATE trigger: raises '
  '"append-only: <table> rows cannot be modified or deleted" (Hard Rule A2).';

COMMENT ON FUNCTION mizan_admin.mizan_chain_link() IS
  'Row-level BEFORE INSERT trigger: requires sequence = max(sequence)+1 and audit_prev_hash = tail audit_hash '
  '(64 zeros on an empty table). Serialises appenders with a per-table advisory lock.';

COMMENT ON FUNCTION mizan_admin.chain_report(text, text) IS
  'Linkage + contiguity report for tenant_<id>.decision_records or control_events. Content hashes are verified '
  'offline with: python -m mizan.audit.verify_chain (Hard Rule A5).';
