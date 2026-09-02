# ADR-0000: Architecture decision records

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Master Plan §8 W0 ("ADR process" is a Sprint-1 foundation, alongside contracts and CI gates)

## Context

The Master Plan and `contracts/` say *what* this system does. Neither says why the alternative was
rejected, and that is the part that gets re-litigated. Four lanes are implementing concurrently against a
frozen contract set; each one will, at some point, hit a rule that looks arbitrary — decimal strings
instead of floats, a `Literal["paper"]` instead of a flag, a pure function instead of an engine object —
and the cheapest thing to do with a rule you do not understand is work around it.

The rules in Master Plan §4 are not arbitrary, but they are stated as assertions. An assertion tells you
what to comply with; it does not tell you what breaks if you do not, so it cannot survive a smart engineer
with a deadline. That is the gap these records fill.

There is a second, narrower need. Invariant tests and contract validators need something stable to cite.
"See the discussion in the pull request" is not a citation; a pull request is a moment, and this project
promises that a decision made today can be explained years later.

## Decision

Numbered Markdown files in `docs/adr/`, one decision each, `NNNN-kebab-case-title.md`, with three
headings — **Context**, **Decision**, **Consequences** — and a header naming the Hard Rule or Risk Canon
rule the decision implements.

Rules of the form:

1. **An ADR is immutable once accepted.** A decision that changes gets a *new* ADR that says
   "supersedes ADR-000N", and the old one gains a `superseded by` line and stays. Deleting the wrong
   answer deletes the reason it was wrong, which is the half a future reader actually needs.
2. **An ADR explains; it does not specify.** `contracts/*.schema.json` and `docs/API-SURFACE.md` are
   binding. Where an ADR and a contract disagree, the contract wins and the ADR is stale.
3. **An ADR cannot override a Hard Rule.** Master Plan §4 is above this directory. An ADR that would
   weaken E1–E9, A1–A6 or B1–B7 is invalid on its face; that change goes through
   `ledger/escalations.md` and a human, not through a document written by the person who wants it.
4. **Name the rule.** Every ADR header cites the Hard Rule or Risk Canon rule it serves, so the
   invariant suite and the ADR set can be read against each other.
5. **Write the rejected option.** An ADR whose Decision section lists no alternative is a description,
   not a decision, and should be a comment in the code instead.

### Alternatives considered and why they were rejected

**Decisions in commit messages.** They are attached to a diff, not to a subject, so the rationale for
"why decimals" ends up split across the eleven commits that touched money. Squash-merging destroys them
outright, and nothing can cite them.

**One `DECISIONS.md`.** Every lane appends to the same file, so every lane conflicts with every other
lane on the same lines — which in practice means people stop appending. It also has no stable anchor: a
test that says "see DECISIONS.md" will point at the wrong section within a month.

**An external wiki.** It drifts, because it is not reviewed in the pull request that changes the
behaviour it describes. The repository is the only place a decision and its code can be wrong together
in a way somebody notices.

## Consequences

- Every ADR is a citable identifier. `docs/API-SURFACE-ADDENDUM-1.md` cites ADR-0006; tests and
  docstrings can do the same, and the citation will still resolve in a year.
- The directory grows monotonically, including with decisions that turned out to be wrong. That is the
  intended cost: the superseded record is where a reader learns which considerations were missed.
- Writing one is a small tax on every architectural choice, which biases toward fewer, larger decisions.
  For this project — where the contracts are frozen and the Hard Rules are not negotiable — that bias is
  the correct one.

### Index

| ADR | Subject | Rule |
|---|---|---|
| [0000](0000-adr-process.md) | This process | W0 |
| [0001](0001-schema-first-contracts.md) | One contract definition; JSON Schemas are generated | A1, W0 |
| [0002](0002-decimal-strings-in-the-decision-path.md) | Decimal strings, never binary floats | A6, C6 |
| [0003](0003-append-only-hash-chained-ledger.md) | Append-only, hash-chained, enforced by the schema | A2, A5 |
| [0004](0004-paper-only-security-boundary.md) | `environment` is a `Literal`, not a flag | B1 |
| [0005](0005-per-tenant-schema-isolation.md) | One schema per tenant, never a query filter | B3 |
| [0006](0006-pure-engine-state-in-context.md) | The engine is pure; state is an input on the context | A1, E2, Risk Canon §14 |
| 0007 | *Folded into ADR-0001.* Generating the JSON Schemas is not a separable decision from choosing which artefact is the single definition; see ADR-0001, "Decision". |
