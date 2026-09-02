# ADR-0004: Paper-only is a type, not a flag — `environment` is a `Literal`

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule B1 (paper and live are **separate deployment and security boundaries, not a
  config flag**), B2 (Mizan never custodies broker keys), E4 (kill switch immediately before the
  mutation); invariant 16 (`no_live_trading_path_exists`)

## Context

This system's worst outcome is not a wrong verdict. It is a real order, with real money, submitted by an
autonomous agent through software that was built and tested for paper trading. Everything else is
recoverable.

Every part of the design that makes Mizan useful also makes that outcome easier to reach: an SDK that
agent frameworks call, adapters that speak a broker's API, an execution gate whose entire job is to submit
orders. The distance between "paper broker" and "live broker" in most client libraries is a base URL.

The instinctive control is a configuration flag defaulting to safe — `paper=True`, `live_enabled=False`.
That is what B1 exists to forbid, and the reason is worth stating precisely rather than treating as
dogma.

## Decision

**`environment` is `Literal["paper"]` in every contract model and `enum: ["paper"]` in every JSON
Schema.** There is no field, flag, constant or code path anywhere in `mizan/` that can express live
trading. The database agrees: ledger tables carry `CHECK` constraints allowing `environment` to be absent
or `'paper'` and nothing else, and the string `'live'` appears in no constraint.

Around that type, in depth:

- `ALPACA_PAPER` must be present and exactly `true`. Absent, empty, `false`, `0`, `no` and `live` all
  raise `LiveTradingForbidden` at configuration time — an unset variable is a refusal, not a default.
- The adapter asserts the broker SDK's base URL is the paper URL. A configuration that looks right but
  points somewhere else fails before the first request.
- Execution defaults submit nothing: `MIZAN_EXECUTION_ENABLED=false` and `MIZAN_EXECUTION_DRY_RUN=true`,
  so two deliberate flips are needed before a *paper* order is submitted.
- The kill switch is read from the environment on every call, immediately before the broker mutation
  (E4), not at request entry.

### Why a `Literal` rather than a config flag

**A flag makes "live" a value the type system can hold.** From the moment it exists, every function that
touches an `environment` has to be audited, forever, for whether it handles that value correctly — and
every future contributor can enable it by editing one line. A `Literal` makes live a *parse error*: the
object cannot be constructed, so there is no code path downstream of it to review, and no line to edit
that would produce a working live order.

**A flag survives serialisation.** A `DecisionRecord` with `environment: "live"` would be a well-formed,
hash-chained, permanently stored record of an event this system claims cannot occur. Because the value is
refused by the pydantic model, by the JSON Schema and by a database `CHECK`, the impossibility is asserted
independently in three places rather than trusted in one — and the two places outside the application
still hold if the application is wrong.

**B1 says "deployment and security boundary", and a flag is neither.** A boundary is something with its
own credentials, its own review, its own blast radius and its own regulatory posture. Compressing that
into a boolean means a person who wants live trading gets it by changing a value, when what they should
have to do is stand up a different deployment and justify it. The mechanism should match the seriousness
of the decision.

### Alternatives considered and why they were rejected

**`environment: Literal["paper", "live"]` with runtime refusal.** The type then permits live and a check
forbids it, which is the flag argument in different clothes: the refusal is one `if` somebody can delete,
and the record shape already allows the value.

**A separate `live` package that simply does not exist yet.** Superficially similar to what we did, but it
leaves the contracts permissive "for later". The contracts are frozen at `1.0.0`; a field that can hold a
value no code produces is an invitation, and it would have to be honoured by every replay of every
historical record.

**Feature-flag service / environment-driven capability.** Adds a network dependency to a
safety-critical property, and makes the safe state depend on something being reachable. Fails open under
exactly the conditions where you need it most.

## Consequences

- **A live build is not a flag away; it is a fork with its own contracts.** That is the intended friction.
  Anyone who wants one must do real work — new contracts, new review, new keys, new deployment — and that
  work is where the scrutiny belongs.
- Tests, fixtures, examples and documentation that say `paper` are not being cautious. It is the only
  value that parses, which is why `tests/infra/test_env_example.py` asserts that no config artefact in the
  repository ever assigns `ALPACA_PAPER` anything but `true`, including in an example.
- Invariant 16 asserts the absence of a live path, and B4 (no cancel/replace automation in v1) narrows the
  mutation surface further: the only broker mutation this system performs is a single order submission.
- Mizan never holds broker keys (B2); they live in the customer's environment. The consequence for
  support is that we cannot reproduce a customer's broker interaction, and that is the correct trade.
