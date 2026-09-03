# ADR-0007: The determinism fingerprint hashes the verdict, not the environment that produced it

- **Status:** accepted
- **Date:** 2026-09-03
- **Implements:** Hard Rule A1 (byte-identical decision replay); milestone M2
- **Applies to:** `scripts/determinism_fingerprint.py`, `determinism-reference.json`, `.github/workflows/determinism.yml`

## Context

The product claim is that the same inputs produce a byte-identical verdict, everywhere, always. The ways
that claim breaks — hash randomisation reordering a map, a comma-decimal locale, a timezone shifting a
timestamp, a different CPU architecture or interpreter build — are all **process or machine properties**.
No in-process test can observe any of them, so the proof has to be a fingerprint computed on several
machines and compared.

That immediately raises the question this record exists to answer: **what goes inside the hashed body?**

The obvious instinct is "everything that could possibly affect the result", which pulls in
`library_versions()` — the exact pydantic, jsonschema and PyYAML builds the decision was computed under.
The first version of the fingerprint did exactly that. It is wrong, and it is wrong in a way that is
invisible on one machine and fatal on six.

## Decision

The hashed body contains **only what the product promises is reproducible**:

```
schema, engine_version, and per scenario:
    evaluation_id, verdict, reason codes, quantities, verdict_hash, policy_hash
```

`library_versions` is recorded in the artifact as **unhashed diagnostics**. `engine_version` stays
**inside** the hash. Identifiers that are random by design — `decision_id`, `auth_id` (uuid7) — are
excluded entirely.

## Why

**Hashing `library_versions` makes the cross-machine gate fail for the wrong reason.** A GitHub
`macos-latest` runner and a `windows-latest` runner routinely resolve different patch releases of a
transitive dependency. If the patch level is inside the hash, every cell disagrees with every other cell
on every run — and disagreement is the exact signal the gate exists to raise. The gate would be
permanently red, would be ignored within a day, and would therefore stop testing determinism at all. A
gate that cannot be satisfied is not a gate; that lesson is already recorded once in this project, in the
REQ-6 lint resolution.

**A pydantic patch release is not a determinism failure.** What determinism means here is that *the
verdict* is identical. If pydantic 2.13.4 and 2.13.5 both produce the same verdict, the system is
behaving exactly as promised, and the fingerprint should say so. If they produce *different* verdicts,
the scenario values diverge and the gate fires on the values themselves — which is the correct signal,
raised for the correct reason, and strictly more informative than "the environments differed".

**Dropping the versions loses nothing, because they are still in the artifact.** They are recorded as
diagnostics precisely so that a genuine mismatch can be attributed: when two runners disagree on a
`verdict_hash`, the first question is "what differed between them", and the answer is in the file. The
information is retained for diagnosis and removed only from the comparison key.

**`engine_version` stays hashed for the opposite reason.** A version bump is *allowed* to change
verdicts. That is a deliberate, reviewed act, and it must invalidate the reference fingerprint so the new
one is regenerated and committed under human eyes. Library drift is not such an act; engine change is.

**The dependency pins already carry the real guarantee.** `pyproject.toml` pins the decision-path
libraries exactly (`pydantic==2.13.4`, `jsonschema==4.26.0`, `PyYAML==6.0.2`) per Hard Rule C6. Version
discipline is enforced there, at install time, where it belongs — not smuggled into a hash whose job is
something else. Enforcing it twice, in one place badly, weakens the place that does it well.

## Consequences

- The determinism matrix can be green across ubuntu/macos/windows and multiple Python minors, which is
  the only configuration in which it proves anything. It was: **6/6 cells, x86 and ARM.**
- A genuine determinism break still fires, and fires on the diverging value with the scenario named.
- **The reference fingerprint must be regenerated whenever the engine's output legitimately changes**, and
  that regeneration is a reviewable diff in `determinism-reference.json`. This has already happened once
  and worked exactly as designed: the INV-25 evidence fix changed `restricted_symbol`'s check content,
  which changed `evaluation_id` and `verdict_hash`, and `--check` reported MISMATCH *before* the commit
  landed. The mismatch was the harness doing its job, not a fault.
- `mizan/replay/__main__.py` compares against the committed reference, so CI and a human on a second
  machine run the identical check by the identical route.

## Alternatives rejected

**Hash `library_versions` too.** Rejected: permanently red across a heterogeneous matrix, for a reason
unrelated to the property under test. This is the option a future reader is most likely to "fix" toward,
which is why this record exists.

**Hash nothing but the verdict strings, dropping `engine_version`.** Rejected: an engine change could
then alter behaviour without invalidating the reference, and the fingerprint would silently bless it.

**Pin the whole environment (lockfile, single Python, single OS) so the versions can be hashed safely.**
Rejected: it would make the matrix agree by construction rather than by evidence. Testing determinism on
one architecture proves the least interesting case. The macOS ARM cell is worth more than five x86 cells,
and it only exists if heterogeneous environments are allowed to agree.
