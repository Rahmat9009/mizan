# `contracts/` — the frozen source of truth

**Schema version:** `1.0.0` · **Status:** FROZEN as of the Sprint 1 checkpoint.

This directory is what stops two halves of a system from being built against two different ideas of the
same object. It is the highest-leverage artefact in the repository and the one thing that must not drift.

## The object chain

```
TradeProposal          what an agent asked for
      |
RiskContext            everything the engine is allowed to know, at one instant
      |
RiskEvaluation         the deterministic verdict  (no LLM in this path)
      |
GovernorDecision       arbitration: deterministic verdict + advisory opinion
      |
ExecutionAuthorization short-lived, single-use, state-bound permission
      |
ExecutionResult        what happened at the gate, and at the broker if it got there
      |
DecisionRecord         one immutable, hash-chained link in the tenant's chain
```

`ControlEvent` is the ninth contract: graduated-response level changes and kill-switch flips, recorded in
the same per-tenant chain as decisions, because a change to the rules is as much a governed event as a
decision made under them.

## Files

| File | What it is |
|---|---|
| `*.schema.json` | JSON Schema draft 2020-12, one per contract, `$id` under `https://mizan.dev/contracts/1.0.0/` |
| `reason_codes.json` | The versioned reason-code taxonomy. Every REJECT and REDUCE cites codes from here (Hard Rule A4) |
| `error_codes.json` | The error taxonomy: stable code, HTTP status, and a safe generic message per class |
| `CANONICAL.md` | Canonical JSON, decimal normalisation, every hash derivation, and how to verify a chain **without Mizan** |

## Schemas are generated, and that is on purpose

The pydantic models in `mizan/contracts/` are the single definition. `scripts/generate_schemas.py`
derives the JSON Schemas from them, and `tests/contracts/` regenerates and diffs on every CI run, so a
model change that is not reflected here fails the build.

This inverts the usual "write the schema, generate the types" mechanic while preserving the entire point
of it. Two hand-maintained artefacts agree on the day they are written and diverge quietly afterwards,
and **a schema that disagrees with the engine is worse than no schema at all, because it is believed.**
One definition, one generator, drift caught by CI.

## Properties every contract holds

- **`additionalProperties: false` everywhere.** An unknown field is a validation error, not a shrug.
- **Money, quantity, price and ratio are decimal strings**, never JSON numbers (Hard Rule A6). A JSON
  number in a money field is rejected.
- **`environment` is `enum: ["paper"]`** — in every object, in every schema. Live trading is a separate
  deployment and security boundary, not a value this type can hold (Hard Rule B1).
- **Missing data is `null`, never `0`.** Unknown risk is not safe risk (Hard Rule E2).
- **The advisory layer has no vocabulary for "more."** `AdvisoryOpinion.recommendation` is one of
  `CONCUR`, `REDUCE`, `REJECT`. There is no value meaning approve-more, and a `GovernorDecision` whose
  authorized quantity exceeds the original is a validation error (Hard Rule E1).
- **`FailClosed.on_missing_market_data` / `on_missing_portfolio_state` / `on_engine_degraded` are
  `Literal[True]`.** The contract cannot express turning them off.
- **`reasoning` is excluded from `proposal_id`.** Free text written by an agent is recorded for audit and
  never reaches enforcement; excluding it from identity makes that structural.

## Changing a contract

Frozen means frozen. A change requires:

1. A HALT entry in `ledger/escalations.md` saying what and why.
2. Orchestrator approval.
3. A schema-version bump — `1.0.0` records already written must keep validating and replaying.
4. Regenerating the schemas and re-running `tests/contracts` and the full invariant suite.

A contract change that would make an existing decision record unreplayable is not a version bump; it is a
different product. Hard Rule A1 does not have an exception for our own convenience.
