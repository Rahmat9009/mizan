# ADR-0002: Money and quantity are decimal strings, never binary floats

- **Status:** accepted
- **Date:** 2026-09-02
- **Implements:** Hard Rule A6 (decimal/fixed-point in the decision path), A1 with Master Plan conflict
  resolution C6 (determinism is over the canonical serialisation), invariant 15
  (`no_binary_float_in_decision_path`)

## Context

Every price, quantity, notional, ratio and greek in this system is compared against a policy threshold,
and the comparison decides whether an order is authorized, reduced or rejected. Two separate defects
follow from representing those values as binary floating point, and they are usually conflated.

The first is arithmetic. `0.1 + 0.2 != 0.3` in IEEE 754, and a position sized at exactly the limit lands
on either side of it depending on the order of operations. At a threshold — which is the only place any of
these values matter — the error is not small relative to the decision.

The second is serialisation, and it is the one that breaks A1 specifically. A `DecisionRecord` is hashed
over its canonical JSON. If a money field is a JSON *number*, then the digest depends on how whatever
parsed and re-emitted that number chose to render it: `2.40` may come back as `2.4`, and a value that has
round-tripped through a double may come back as `2.4000000000000004`. Different languages, different
parsers, different answers. A5 says a customer must be able to verify our chain with their own tools; if
their JSON library renders a number differently from ours, they compute a different hash and conclude the
record was tampered with. The chain would be reporting a forgery that did not happen.

## Decision

`DecimalStr` — a JSON **string** matching `^-?(0|[1-9]\d*)(\.\d+)?$`. No exponent, no leading `+`, no
leading zeros, no separators. Normalised at validation time, **before any hash is taken**: `"2.40"` →
`"2.4"`, `"100.00"` → `"100"`, `"-0"` → `"0"`.

Concretely:

- **JSON numbers are rejected, not coerced,** for every `DecimalStr` field. A number in a money field is
  a validation error with a reason code, not a silent conversion.
- Arithmetic uses `decimal.Decimal` inside `DECIMAL_CONTEXT` — `prec=28`, `ROUND_HALF_EVEN`, and traps on
  `InvalidOperation`, `DivisionByZero` and `Overflow`.
- `canonical_json` raises `TypeError` on `float`, `NaN` and `Infinity`. There is no path by which a float
  reaches a hash.
- `mizan/contracts`, `mizan/policy`, `mizan/risk`, `mizan/governor`, `mizan/authorization`, `mizan/audit`
  and `mizan/replay` contain no `float` name, no float literal and no `math` import. Invariant 15 scans
  for exactly that, so the rule cannot decay by accretion.

The normalisation is what makes the useful property true: **two proposals that differ only in how their
money was spelled are the same proposal and hash identically.** `limit_price "2.40"` and `"2.4"` produce
the same `proposal_id`, so idempotency (E7) and replay (A1) both work on values that arrived from
different clients with different formatting conventions.

### Alternatives considered and why they were rejected

**Floats, rounded at the boundary.** The rounding is itself a decision — to how many places, in which
direction, at which step — and rounding is where the money goes. Worse, it makes A1 unprovable rather
than merely fragile: two builds on different platforms can differ in the last bit, and at a threshold that
is a different verdict and a different reason code, from identical inputs. E5 ("no silent resizing") also
becomes meaningless when the authorized size is approximate: the gate cannot distinguish a genuine change
in state from a representation artefact.

**Integer minor units — everything in cents.** This is the standard answer in payments and it is a good
one there. It does not survive contact with this domain: option greeks, ratios (`max_single_symbol_pct`),
per-share prices quoted in sub-cent increments and fractional quantities all need a scale, and a per-field
scale has to be transmitted, agreed and versioned, which is a second contract nobody wants to maintain.
It also solves only the arithmetic half; the wire representation is still a JSON number.

**JSON numbers with arbitrary-precision parsing.** Some parsers can be configured to hand you a decimal
rather than a double. Most consumers will not configure theirs, and we do not control the consumer — an
auditor re-hashing an export uses whatever `json` module their language ships. A string is the only JSON
representation that every implementation round-trips byte-for-byte, and byte-for-byte is precisely the
guarantee A5 sells.

**Decimal in Python, but numbers on the wire.** Half a fix. The hash is taken over the wire form, so the
wire form is what has to be canonical.

**Rounding instead of trapping.** `DECIMAL_CONTEXT` traps `InvalidOperation` and `DivisionByZero` rather
than returning `NaN`. A degenerate computation — a ratio against a zero denominator, say — must fail
closed and block, not yield a plausible number that passes a limit check. That is E2 applied to
arithmetic: unknown is not safe.

## Consequences

- Arithmetic is explicit and slightly verbose. Every boundary between the wire and the engine goes through
  `dec()` and `dstr()`; there is no implicit coercion, on purpose.
- Anyone reimplementing verification **must normalise before hashing**, or they will compute different
  digests from ours for values we consider equal. `contracts/CANONICAL.md` §2 states the normalisation
  rules and gives worked examples for this reason.
- Comparisons in policy evaluation are decimal comparisons against decimal thresholds, so a threshold
  written `"0.10"` and one written `"0.1"` are the same threshold and produce the same `policy_hash`.
- The prohibition is enforced by a test that reads source, not by discipline. That test will occasionally
  be annoying — it flags a `float` in a docstring example or an unrelated helper — and the correct
  response is to move the helper out of the decision path, not to relax the scan.
