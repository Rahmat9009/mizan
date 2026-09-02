# ADR-0001: One contract definition — pydantic models, generated JSON Schemas

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule A1 (same inputs + same policy + same engine = same verdict), Master Plan §5
  ("contracts — lock before any implementation"), §8 W0 (contract tests as a CI gate)
- **Absorbs:** what would have been ADR-0007, "generated JSON schemas"

## Context

Nine objects — `TradeProposal`, `RiskContext`, `Policy`, `RiskEvaluation`, `GovernorDecision`,
`ExecutionAuthorization`, `ExecutionResult`, `DecisionRecord`, `ControlEvent` — are the entire interface
between parts of this system that are built separately: a deterministic engine, an execution gate, broker
adapters, an SDK, external agent frameworks, and whatever a customer writes against the published
schemas. Master Plan §5 calls the contract set "the single highest-leverage artifact", and it is right for
an unglamorous reason: two halves of a system built against two subtly different ideas of the same object
fail in production, at the boundary, under load, with no local reproduction.

A1 raises the stakes past ordinary interface drift. A `DecisionRecord` written today must still validate
and still replay to the same verdict much later. That is only meaningful if there is exactly one
authoritative statement of what the record's shape *was*, and if that statement cannot silently disagree
with the code that produced the record.

W0's original sketch said "contracts in protobuf/OpenAPI, codegen both sides", i.e. schema-first. That is
the conventional answer, and it is the one we did not take.

## Decision

**The pydantic v2 models in `mizan/contracts/` are the single definition.** `scripts/generate_schemas.py`
derives `contracts/*.schema.json` (JSON Schema draft 2020-12, `additionalProperties: false` throughout,
`$id` under `https://mizan.dev/contracts/1.0.0/`) from those models. `tests/contracts/` regenerates the
schemas on every CI run and diffs them against what is committed, so a model change that is not reflected
in the committed schemas fails the build.

This inverts the usual schema → types codegen while preserving the property that mechanic exists to
provide: **one definition, one generator, drift caught by machine rather than by review.**

The generated schemas are committed rather than built on demand, because they are the artefact an
external integrator reads and pins, and because a diff in a pull request is the moment a contract change
becomes visible to a human. They are read-only: editing `contracts/*.schema.json` by hand is a defect, and
CI reports it as one.

### Alternatives considered and why they were rejected

**Two hand-maintained artefacts — write the JSON Schema, write the models.** This is the common shape and
it fails in a specific, predictable way: the two agree on the day they are written and diverge quietly
afterwards, because nothing forces them to be edited together. The divergence is discovered by an
integrator whose payload validates against the published schema and is then rejected by the engine, or —
worse — accepted with a field the engine ignores. As `contracts/README.md` puts it: *a schema that
disagrees with the engine is worse than no schema at all, because it is believed.*

**Schema-first with generated Python types.** Attractive until you look at what these contracts actually
assert. `proposal_id == proposal_id_for(model_dump)`. Decimal strings normalised before any hash is taken.
`REJECT ⇒ recommended_quantity == "0"`; `REDUCE ⇒ 0 < recommended < original`. Leg count determined by
strategy. `expires_at > created_at`. Any failed blocking check forces `REJECT`. JSON Schema can express
almost none of that, so the validators would remain hand-written *beside* the generated types — which
reintroduces the two-artefact drift problem one layer down, in the place where it is hardest to see. The
behaviour is the contract, not the field list, so the definition has to live where the behaviour lives.

**Protobuf.** It gives strong codegen across languages, and we still declined it. A5 requires a customer
to verify chain integrity **without Mizan** — `contracts/CANONICAL.md` §6 shows the complete verifier in
about twenty lines of Python standard library, using `json.dumps(sort_keys=True)`. That is only possible
because the canonical form is JSON with sorted keys, which every language reproduces identically.
Protobuf's serialisation is not canonical across implementations (field ordering, default handling and
varint packing are implementation choices), so an auditor's re-encode could disagree with ours and the
hash would differ for reasons that have nothing to do with tampering. A binary wire format also makes the
stored record unreadable without tooling, which is the wrong property for an audit artefact.

**OpenAPI as the source of truth.** Same objection as schema-first, plus it centres the HTTP surface. The
contract set is not an API description; the engine, the ledger and the replay path use these objects with
no HTTP anywhere.

## Consequences

- `contracts/*.schema.json` is downstream and read-only. The only supported edit path is: change the
  model, run `python scripts/generate_schemas.py`, commit both.
- `scripts/generate_schemas.py` is on the critical path of CI. A bug in the generator is a contract bug,
  and it is tested as one by `tests/contracts/` (42 tests at the Sprint-1 freeze).
- Non-Python consumers get schemas, not types, and must implement the derived rules themselves. That is
  what `contracts/CANONICAL.md` is for: it documents canonical JSON, decimal normalisation, every hash
  derivation and the chain rules precisely enough to reimplement.
- Changing a contract now costs a schema-version bump and a HALT entry (`contracts/README.md`,
  "Changing a contract"), because records already written must keep validating and replaying. This is
  friction by design; A1 has no exception for our own convenience.
- Two artefacts still exist in the tree, so somebody will eventually edit the wrong one. The CI diff is
  what makes that a failed build in minutes instead of a support incident in months.
