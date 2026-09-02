# memory.md — distilled state for a fresh agent (rewritten each sprint; ≤200 lines)

## Read first
1. CURRENT_AIM.md (never rewritten)  2. ledger/escalations.md  3. docs/API-SURFACE.md + docs/API-SURFACE-ADDENDUM-1.md
4. docs/MIZAN-MASTER-PLAN-v2.md §4 §5 §8  5. docs/MIZAN-RISK-CANON.md  6. docs/MIZAN-KILLER-FEATURE-VERDICT.md (truncated)

## Where things are
- Repo root = c:/Users/intel/Downloads/OU/projects/mizan, git branch `main`, remote origin = Rahmat9009/Mizan- (default `master`). Never push.
- New core: `mizan/` package. Legacy: `app/` (Rahmat) — read-only salvage; its 354 tests must stay green.
- Frozen after Sprint 1: `contracts/`, `tests/invariants/`. Changes need HALT + ledger/escalations.md.
- State files: ledger/progress.md, ledger/learnings.md, ledger/escalations.md, ledger/requests.md, security/findings.md.

## Lane ownership (write paths)
L0 contracts/ mizan/contracts tests/contracts tests/fixtures pyproject Makefile .github infra docker-compose scripts docs/adr
L1 mizan/policy mizan/risk tests/policy tests/risk
L2 mizan/governor mizan/advisory mizan/authorization mizan/audit mizan/replay + tests/<same>
L3 mizan/execution mizan/adapters mizan/sdk mizan/api + tests/<same>
L4 mizan/console tests/console
L5 security/ tests/security (flag only)      L6 tests/integration ledger/ (propose patches only)

## Architecture decisions that must not be re-litigated
- Engine is a pure function evaluate(proposal, context, policy). ALL state (path, aggregate, agent, response level, calendar)
  is a RiskContext input, captured in the record, replayed exactly. (ADR-0006)
- Money/quantity/ratio = DecimalStr JSON strings; JSON numbers rejected; no `float` in the decision path.
- Time is an input (context.evaluated_at); only the execution gate reads an injectable clock.
- proposal.reasoning is audit-only; only mizan/advisory, mizan/audit, mizan/console may read it.
- Advisory can only CONCUR/REDUCE/REJECT; governor clamps; contract types cannot express "more".
- Ledger: append-only at storage level (SQLite triggers now; Postgres triggers in infra/), one chain per tenant,
  ZERO_HASH start, audit_hash = sha256(canonical(record minus audit_hash)). Verification is offline-capable.
- Paper only: every `environment` enum == ["paper"]; ALPACA_PAPER != true → LiveTradingForbidden at construction.
- Execution gate order: enabled → auth valid → idempotency → TOCTOU re-evaluate (+response level) → consume auth →
  auth valid again → KILL SWITCH LAST → dry_run/submit. Never resize (E5).
- Unimplemented-but-enabled policy check → refused at load (CHECK_NOT_IMPLEMENTED). Missing state for enabled check → REJECT.

## Environment
Python 3.12.4, pytest 9, pydantic 2.13.4, jsonschema 4.26, hypothesis 6.157, ruff. Docker daemon NOT running (compose untested).
No gh CLI. Run tests with `python -m pytest -q -p no:cacheprovider <dir>`.

## Sprint log (one line each)
- S1: L0 (contracts/invariants/infra) + L5 sweeps 1–2 dispatched in parallel. Companion docs arrived; Addendum 1 applied pre-freeze.

## Open items for the next sprint
- Verify L0 output, run contracts+infra+invariants(pending) + legacy suite, FREEZE contracts/, create worktrees, dispatch L1–L6.
