# ADR-0005: One schema per tenant, one file per tenant — never a query filter

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule B3 (cross-tenant access impossible by construction — **separate schemas
  minimum, not query filters**), A2 (the isolation and the append-only guarantee share one mechanism);
  invariant 12 (`cross_tenant_access_is_impossible`)

## Context

Each tenant's ledger contains their trading decisions, their positions, their agents' reasoning and their
policy thresholds. A cross-tenant read is not a bug of the ordinary kind; it is one customer seeing
another customer's strategy, and it is unrecoverable the moment it happens once.

The default industry answer is a `tenant_id` column and a `WHERE tenant_id = ?` on every query, usually
wrapped in a base repository class so nobody has to remember it. B3 rejects that answer, and the reason
is a statement about what can be *tested*, not a preference about database design.

## Decision

**One PostgreSQL schema per tenant.** `tenant-a` → schema `tenant_tenant_a`, role `tenant_tenant_a_app`.
Provisioning is a `SECURITY DEFINER` function, `mizan_admin.create_tenant(tenant_id)`, owned by a
non-superuser admin role, which creates the schema, the role, the ledger tables, the append-only and
chain-linking triggers and the grants — idempotently.

The tenant's application role is `NOLOGIN`, owns nothing, and holds `USAGE` on **its own schema only**
plus `SELECT` and `INSERT` on its own tables. It has no `USAGE` on `mizan_admin`, so it cannot even
enumerate the list of tenants. Every table additionally carries `CHECK (tenant_id = <this tenant>)`, so a
row cannot claim to belong to somebody else even within the schema it is allowed to write.

**The SQLite ledger follows the same rule:** one database file per tenant, not one file with a tenant
column. It is the Sprint-1 tested persistence, and it must not encode a weaker isolation model than the
one it will be replaced by.

Tenant ids match `^[a-z0-9][a-z0-9-]{0,62}$` (the contracts' `TenantId`) and are additionally capped at
52 characters, because `tenant_<id>_app` must fit PostgreSQL's 63-byte identifier limit. Without that cap,
two long tenant ids could truncate to the *same role name* — silently merging two tenants' privileges,
which is precisely the failure this ADR exists to prevent. `-` maps to `_`, and since tenant ids cannot
contain `_`, the mapping is injective.

### Alternatives considered and why they were rejected

**A `tenant_id` column with query filters.** This makes isolation a property of every query that will
ever be written, forever. One forgotten `WHERE` in one reporting endpoint, one ORM relation that loads
without the filter, one raw SQL string in a migration, one `JOIN` whose filter applies to the left side
only — and the breach is silent and total. The decisive point is testability: **there is no test that
proves the absence of a bad query.** You can review the queries that exist today; you cannot review the
one somebody adds next quarter. There *is* a test that proves a role has no `USAGE` on another tenant's
schema, and it stays true no matter what queries get written, because the database refuses them.

**Row-level security.** Genuinely better than manual filters, and still one misconfiguration away: a
policy that is `PERMISSIVE` where it should be `RESTRICTIVE`, a role with `BYPASSRLS`, a `SECURITY
DEFINER` function that runs as the owner (for whom RLS is disabled by default), a table created later
without RLS enabled. The roles here are explicitly `NOBYPASSRLS` and own nothing, but the more useful
property is that schema isolation fails *closed and loudly* — the object simply is not visible — whereas
an RLS misconfiguration fails open and silently.

**A database per tenant.** Stronger, and we did not choose it as the baseline for cost reasons that are
about operations rather than security: connection pooling per database, migrations applied N times, and
no single `create_tenant` provisioning path. B3 names "separate schemas" as the *minimum*, and because the
schema name is the only thing the application resolves, a customer who requires database-per-tenant can be
moved there without an application change. Keeping that upgrade path cheap was part of the decision.

**Application-level tenancy middleware.** Same class of objection as query filters, one layer higher: it
protects the paths that go through it.

## Consequences

- **Provisioning a tenant is DDL, not an `INSERT`.** Adding a tenant requires membership of
  `mizan_admin`; the application cannot do it, and a compromised application therefore cannot create a
  schema it would then have rights to.
- **Cross-tenant reporting is an explicit admin-side operation** — a union performed by a role that holds
  the privilege — rather than something that happens by relaxing a filter. Aggregate product features must
  be designed as admin surfaces from the start.
- **The tenant id charset is frozen** by two independent artefacts that must agree: the contracts'
  `TenantId` regex and the DDL's `CHECK` and function guard. `tests/infra/test_postgres_sql.py` asserts
  the DDL regex is present and anchored, and exercises it against injection-shaped and
  overlong candidates — an unanchored regex would accept `a; DROP SCHEMA public`, and the identifier is
  interpolated into DDL.
- **Isolation and append-only share one mechanism.** The same "the app role owns nothing" property that
  keeps a tenant out of another schema also keeps it from dropping its own append-only triggers (ADR-0003).
  One privilege model, two Hard Rules.
