# Learnings ledger (append-only)

- [2026-09-02T14:32:10Z] S1 | Legacy `app/` is a competent single-tenant equities governor with an options foundation, but it violates A6 (floats in the decision path), A2/A5 (no hash chain; mutable upserts on execution_results/broker_orders), E4 (kill switch read once at config load), B3 (no tenant boundary). Decision: keep `app/` untouched as salvage reference; build the new core under the `mizan/` package. The options package (Decimal, pure) is the best salvage candidate.
- [2026-09-02T14:32:10Z] S1 | Integration branch is `main` locally; remote default is `master`. Nothing is pushed by the Orchestrator. Human pushes `main:master` when ready.
- [2026-09-02T14:32:10Z] S1 | Lane ownership paths are mapped onto the single `mizan/` package: L1 mizan/policy mizan/risk · L2 mizan/governor mizan/advisory mizan/authorization mizan/audit mizan/replay · L3 mizan/execution mizan/adapters mizan/sdk mizan/api · L4 mizan/console. Tests follow the same split under tests/<lane-dir>/.
- [2026-09-02T14:32:10Z] S1 | Invariant bootstrap policy: tests/invariants/ is authored in S1 against the API surface in docs/API-SURFACE.md and is red until lanes implement. At each checkpoint an invariant that is red ONLY because its subject module still raises NotImplementedError is "pending", not "blocking". Any other red invariant blocks every merge. No invariant may be edited to go green.
- [2026-09-02T14:41:09Z] S1 | Companion docs received mid-sprint (Risk Canon, Killer Feature Verdict). Architectural resolution of Canon §14 P0: the risk engine remains `evaluate(proposal, context, policy)` with NO hidden state; `RiskContext` gains `path_state`, `aggregate_state`, `agent_state`, `response_level`, `calendar`, liquidity fields, and the authorization gains `bound_state` (continuous state-bound authorization). The context provider (L3) assembles that state from ledger + broker; the record captures it; replay reuses it. Missing state for an enabled check → REJECT (E2 extended per R-RUIN-4). Unimplemented-but-enabled check → POLICY_INVALID/CHECK_NOT_IMPLEMENTED at policy load (fail closed, never skip).
- [2026-09-02T14:41:09Z] S1 | Vocabulary: say "decision replay" / "governance replay", never bare "replay", in user-facing text (naming collision with retail chart-replay tools). No competitor claim without a working link.
- [2026-09-02T22:57:44Z] S1 | All four Sprint-1 agents died simultaneously on one shared session rate limit. Lesson: a rate limit is a SHARED failure domain, not an independent per-agent risk — parallel fan-out does not survive it. Mitigation adopted: the Orchestrator finishes the critical path itself when agents die, and dispatch waves are kept smaller with the highest-value lane first.
- [2026-09-02T22:57:44Z] S1 | Two genuine bugs were caught by the invariant suite before any lane code existed, which is the whole argument for writing invariants first: (1) canonical_json refused Decimal, which would have forced every caller to stringify by hand and invited representation drift in hashes; (2) the evaluation fixture always emitted a full recommended_notional, so every REJECT built from it violated its own contract. Neither was found by the code that produced them.
- [2026-09-02T22:57:44Z] S1 | Contracts are GENERATED from the pydantic models by scripts/generate_schemas.py rather than hand-written alongside them. Two hand-maintained artefacts agree on day one and silently diverge after; a schema that disagrees with the engine is worse than none because it is believed. tests/contracts/test_schema_generation.py (Sprint 2) regenerates and diffs so drift fails CI. This inverts "schema-first" in mechanism while preserving its purpose: one definition, no drift.
- [2026-09-02T23:05:43Z] S2 | The contracts freeze-guard test was wrong twice before it was right, and each correction taught the schema shape: contract objects come in THREE legitimate forms — closed records (properties + additionalProperties:false), pattern maps (patternProperties with $ref values + additionalProperties:false, the strictest), and open maps (typed additionalProperties, for check ids / provider names). A blanket "additionalProperties must be false" rule would have forced the maps to be unusable or the rule to be waived. Correcting a mis-stated test is not weakening it; the underlying property (no content goes unvalidated) is now actually enforced in all three shapes.
- [2026-09-02T23:05:43Z] S2 | Documentation that claims a verifiable property must be executed, not asserted. The standalone verifier printed in CANONICAL.md §6 was run against a good chain, a tampered chain and a chain with a deleted record before the document was committed. A customer-facing verification procedure that does not actually work is worse than none.
- [2026-09-03T00:12:52Z] S2 | Value-pattern redaction is genuinely dual-edged and needs the same care as an enforcement rule. Adding regex scrubbing closed the one real credential path (a secret pasted into free text, unreachable by key matching because every model forbids extra fields) but immediately created an availability bug: the Alpaca key shape collides with OCC option symbols for tickers beginning AK or PK, and since occ_symbol is contract-validated, redacting it makes the record unbuildable. A redactor that is too eager does not merely over-hide, it can take a whole class of instrument offline. Both directions now have tests.
- [2026-09-03T00:12:52Z] S2 | Two collisions of the same kind in one sprint (CalendarState.session vs the "session" key pattern, occ_symbol vs the Alpaca key pattern) say the pattern is systemic: a redactor matching on NAMES or SHAPES will eventually collide with a contract field, and the failure is silent-until-total. The durable fix that landed is L2b's test walking all 304 field names of all 60 contract models and asserting the collision set is exactly the two that are deliberately handled — a new sensitive pattern now fails there rather than in production.
- [2026-09-03T00:12:52Z] S2 | Reviews caught what tests did not. Both collisions were found by an agent reading the change, not by any suite; the second was reported as "not raised as a REQ since it's your call". Worth keeping: a lane that finishes should read the shared code its work depends on.

## Sprint 3 — [2026-09-03T01:40:00Z]
- **A redactor that matches on key NAMES will eventually collide with a contract FIELD, and the failure is silent until it is total.** Two collisions landed this sprint and each one stopped records validating outright: `CalendarState.session` against the new `session` key pattern, and `occ_symbol` against the Alpaca `PK|AK` key pattern (AKAM, PKG). Both were found by reading, not by testing. The durable guard is L2b's test that walks all 304 field names across all 60 contract models and asserts the collision set is exactly the two deliberately handled ones. **Keep that test** — it is what stops the third collision. Credit: mizan-04's L2b lane, carried over at handover.
- **Exempting a path from a gate must exempt every rule the gate can raise there, not just the cosmetic ones.** The REQ-6 lint resolution kept ruff's `B` rules enforced on frozen paths, so `B905` fired inside `tests/invariants/` and a lane did the reasonable thing and edited a frozen file to satisfy the linter it was shown. A gate that reports a fixable finding in a file nobody may edit will keep manufacturing exactly that violation.
- **Two sessions in one working tree is a class of failure no lane discipline can reach.** §5.2 assigns one owner per path, but it assumes one dispatcher. With two, both sides' agents obeyed their briefs perfectly and still collided. What saved it was that both sides independently converged on self-contained test modules, reducing the collision surface to whole-file granularity. What did NOT save it was the commit discipline: `git add -A` mid-sprint produced a red commit from a green tree.
- **`git add -A` is never a checkpoint.** A checkpoint commit must name its paths, and must be gated in a CLEAN CLONE, not in the working tree that produced it. The working tree passed while the commit it produced failed — the difference was uncommitted files the tree had and the commit did not.

## Sprint 3 — L6 (critic & integration)

- **A freeze on a directory has to say whether adding a file to it is a change.** Two of the four
  frozen-path hunks this build produced were NEW documentation files added to `contracts/`. Both were
  announced in progress.md and neither was escalated, because "don't edit the frozen files" reads as a
  rule about modification. State the rule as "no write of any kind under these paths", or name the
  artefacts rather than the directory. The same ambiguity will otherwise recur at every schema bump.
- **Verify an escalation's claim rather than reading it.** ESC-1 says the removed hypothesis inputs
  were unconstructible; the B905 entry says `strict=False` is a no-op. Both are true, and both took
  one line to confirm. An audit that accepts a well-written justification is not an audit — it is a
  second reading of the same sentence, and the whole reason the freeze exists is that the persuasive
  explanation is what a real weakening would also come with.
- **The strongest thing about the redaction rework is a choice in the test, not in the code.** The
  key-based tests assert with a value that is deliberately NOT credential-shaped, so they cannot pass
  because the value scrubber happened to catch it. When a system has two overlapping defences, a test
  that would pass under either one proves neither. This is the pattern to copy.
- **`passed=True, severity="blocking"` is a claim, and a check with no input must not make it.**
  ESC-4: `duplicate_order` reads `RiskContext.recent_orders`, which nothing populates, so it reports
  a blocking control that held — in a record whose entire purpose is to be believed later. The engine
  gets this right everywhere it can see the gap (`aggregate_exposure` blocks with
  `AGGREGATE_STATE_MISSING`; deferred checks record `info` with a detail). The failure was not a
  missing rule, it was that nobody asked whether the check's input was ever wired. **For every check
  that reads a context field, assert somewhere that the shipped pipeline actually populates it.**
- **Prove ordering rules by ordering, not by outcome.** Hard Rule E4 is about *where* the kill switch
  is read, and "it blocked" is true of an implementation that reads it at entry. Giving the kill
  switch and the broker one shared log turns E4 into a one-line assertion about a list, and it catches
  the caching mistake that the outcome assertion cannot.
- **Run the published algorithm, not a paraphrase of it.** `contracts/CANONICAL.md` §6 prints a
  standalone verifier and claims it is the whole of the algorithm. Transcribing that snippet into a
  test — importing nothing but `hashlib` and `json` — and running it against a real chain is the only
  thing that keeps a customer-facing specification honest as the code moves. Do the same for every
  worked example a document publishes; two of CANONICAL.md's digests are now under test.
- **Write a characterisation test so that it fails when the defect is fixed.** The four ESC-4 tests
  assert the broken behaviour with messages that say "ESC-4 may be fixed; update this test". A defect
  pinned that way cannot be quietly fixed, cannot be quietly forgotten, and does not turn into a test
  that defends the bug.
- **A demo that runs is not a demo that proves.** Checking `examples/killer_demo.py` bullet by bullet
  against CURRENT_AIM.md found three real gaps (no REDUCE beat, an in-memory ledger behind an
  "append-only" claim, and Alpaca never reached) that reading the script does not surface, because the
  script's own headings assert each bullet. Assert the negatives too, so closing a gap is announced.
- **Two git remotes now exist, and the asymmetry is the whole point.** `origin` (Rahmat9009/Mizan-) is the
  SOLE SOURCE OF TRUTH; `ci` (faisalhacks/mizan) is a RUNNER, not a second home, added only because there is
  no admin access on origin to run the determinism matrix. Rules: push order is always origin first then ci;
  never pull or fetch from ci; nothing is ever authored on the mirror; if they disagree, origin wins.
  Recorded here because undocumented parallel state is exactly what caused the two-orchestrator collision
  (ESC-3) - the same mistake at repository level would be worse, because a divergent mirror looks authoritative.
