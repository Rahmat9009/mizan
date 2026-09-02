# ADR-0003: An append-only, hash-chained ledger, enforced at the database schema level

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule A2 (append-only, hash-chained; **no update path, no delete path, at any
  privilege level — enforced at the database schema level**), A5 (chain integrity independently
  verifiable by the customer without Mizan's involvement), A3 (recursive redaction before persistence);
  invariants 8, 9 and 10

## Context

The audit trail is the product. Everything else Mizan does — evaluate, arbitrate, authorize — is only
worth anything if the record of what happened cannot be quietly edited afterwards. The regulatory framing
in Master Plan §3 is explicit that the customer, not Mizan, is the one who has to be able to demonstrate
control, which means the customer has to be able to check our claims without asking us.

That sets two requirements that pull in different directions. The record must be hard to alter, and it
must be trivially easy to *verify* — because a verification procedure that requires our software, our
keys or our cooperation is not evidence for the customer, it is a promise from us.

## Decision

**One hash chain per tenant.** Records are numbered from `1` and never renumbered.
`audit_prev_hash` of record 1 is the `ZERO_HASH` (64 zeros); of record *N*, the `audit_hash` of record
*N−1*. `audit_hash = sha256(canonical_json(record without "audit_hash"))`. A chain is valid when, for
every record in sequence order: the content hashes to its stored `audit_hash`, the sequence is exactly one
more than its predecessor's, and the previous-hash links. A verifier reports **the first sequence number
that fails** — "broken at record 47" is useful to an auditor in a way that "broken" is not.

`ControlEvent` records — graduated-response level changes and kill-switch flips — go in the same
per-tenant chain (Risk Canon R-GRAD-2), because a change to the rules is as much a governed event as a
decision made under them.

**Redaction happens before hashing** (A3): the hash covers the redacted record, which is the record
actually stored. A verifier re-hashing what it was given agrees, and no verifier ever needs to see a
credential to check integrity.

**Enforcement lives in the database schema, not in the application.** In `infra/postgres/init/`:

- Statement-level `BEFORE UPDATE OR DELETE OR TRUNCATE` triggers on every ledger table, raising
  unconditionally. Statement-level rather than row-level for two reasons: `TRUNCATE` has no row-level
  trigger at all, and `UPDATE ... WHERE false` matches no rows — both must still be refused.
- A row-level `BEFORE INSERT` trigger on the chained tables requiring `sequence = max(sequence) + 1` and
  `audit_prev_hash = ` the current tail's `audit_hash`, with a per-table advisory lock so two concurrent
  appenders cannot both read the same tail.
- A per-tenant application role that is `NOLOGIN`, owns nothing, holds `SELECT` and `INSERT` only, and is
  explicitly revoked `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` and `TRIGGER`. Owning nothing is the
  load-bearing part: the role cannot drop or disable the trigger that constrains it.

**Verification is twenty lines of standard library.** `contracts/CANONICAL.md` §6 contains a complete
independent verifier: read a JSON-lines export, re-canonicalise each record, compare. No Mizan import, no
key, no service.

### Alternatives considered and why they were rejected

**Enforce append-only in application code.** A repository class with no `update()` method is a
convention, and A2 says "at any privilege level". Application-level enforcement is one migration script,
one admin console, one `psql` session or one ORM `save()` away from being bypassed, and the bypass leaves
no trace — the row simply has different contents than it used to. Putting the refusal in the schema
changes the attack from "run an UPDATE" to "acquire DDL rights on a table you do not own and drop a
trigger", which is a different kind of act and one that shows up in server logs.

**Sign each record with a Mizan-held key.** This proves Mizan wrote the record. It does not prove Mizan
did not rewrite it, because Mizan still holds the key — it relocates the whole question to key custody,
which is exactly what B2 says we do not want to be responsible for.

**A blockchain or an external notary as the primary mechanism.** Distributed consensus buys agreement
among mutually distrusting participants; here there are none, and the "participants" would be one
customer's own database. It also pushes the customer's trading decisions outside their boundary, which
runs against B2 and B3. Periodic *anchoring* — publishing the latest `audit_hash` somewhere the operator
does not control — is a real and cheap countermeasure and is discussed below, but it is an addition, not
the foundation.

**Soft deletes / a `deleted_at` column.** An update path with a friendly name.

## What this does not prove

Stated plainly, because overclaiming here is the fastest way to lose an auditor
(`contracts/CANONICAL.md` §5):

A hash chain provides **tamper evidence**. It shows that the stored records have not been altered, removed
or reordered since they were written — *unless whoever altered them also recomputed every subsequent
hash*.

It is **not a blockchain**, and there is no distributed consensus. Someone with write access to the
database and the ability to rewrite the whole chain forward can produce a self-consistent forgery. Two
countermeasures narrow that, and neither is claimed unless it is actually deployed:

1. **The database triggers.** They raise the bar from "update one row" to "obtain DDL privileges the
   application role does not have, drop a trigger, rewrite N records, restore the trigger."
2. **Anchoring.** Periodically publishing the latest `audit_hash` somewhere the operator does not control
   pins everything before it: a forgery must now also alter a record outside the operator's reach.

Tamper evidence is a strong, honest property. Describing it as immutability is neither, and a customer's
auditor will find the difference before we do.

## Consequences

- **A state change is a new row, never an edit.** `execution_results` is an event log: a fill after a
  submission is another record, not an `UPDATE` of `status`. A mistake is corrected by a compensating
  record that supersedes it; the mistake stays in the chain, which is the point.
- **Appends are serialised per tenant table.** The chain-link trigger takes a transaction-scoped advisory
  lock, so one writer per tenant table at a time. At pre-trade decision rates this is not a constraint,
  and it is the price of guaranteeing contiguity — without it, two concurrent inserts read the same tail
  and one of them writes a broken link.
- **Nothing can be renumbered.** Backfilling a record "in the right place" is impossible by construction,
  which occasionally makes an operational fix awkward and always makes the chain meaningful.
- **The DDL is the security boundary**, so it is reviewed as one: `tests/infra/test_postgres_sql.py`
  asserts the triggers, the grants and the revokes statically, and the CI `postgres-ddl` job applies the
  files to a real PostgreSQL and runs `infra/postgres/verify/prove_append_only.sh`, which attempts every
  forbidden statement as both the superuser and the tenant application role and requires each to fail.
