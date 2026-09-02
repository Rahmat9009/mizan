# memory.md — distilled state for a fresh agent (rewritten each sprint; ≤200 lines)

## Read first
1. `CURRENT_AIM.md` (never rewritten)  2. `ledger/escalations.md`  3. `docs/API-SURFACE.md` + `docs/API-SURFACE-ADDENDUM-1.md`
4. `contracts/CANONICAL.md`  5. `docs/MIZAN-MASTER-PLAN-v2.md` §4 §5 §8  6. `docs/MIZAN-RISK-CANON.md`
7. `docs/MIZAN-KILLER-FEATURE-VERDICT.md` (arrived truncated after §7 — see escalations)

## Where things are
- Repo root `c:/Users/intel/Downloads/OU/projects/mizan`, branch `main`, remote `Rahmat9009/Mizan-` (default `master`). **Never push.**
- New core: `mizan/`. Legacy: `app/` (Rahmat) — read-only salvage, its 354 tests must stay green.
- **FROZEN at commit 9eebf24:** `contracts/`, `tests/invariants/`. Changes need a HALT entry in `ledger/escalations.md` + Orchestrator approval.
- State files: `ledger/{progress,learnings,escalations,requests}.md`, `security/findings.md`, this file.

## Lane ownership (write paths)
```
L0  contracts/ mizan/contracts tests/contracts tests/fixtures tests/infra pyproject Makefile .github infra scripts docs/adr
L1  mizan/policy mizan/risk policies/ tests/policy tests/risk
L2a mizan/governor mizan/advisory mizan/authorization tests/{governor,advisory,authorization}
L2b mizan/audit mizan/replay tests/audit tests/replay
L3  mizan/execution mizan/adapters mizan/sdk mizan/api tests/{execution,adapters,sdk,api}
L4  mizan/console tests/console          L5 security/ tests/security (FLAG ONLY, no edits elsewhere)
L6  tests/integration ledger/ (propose patches only)
```
Need a change outside your paths? Append to `ledger/requests.md`. Never reach across.

## Architecture decisions — settled, do not re-litigate
- **Engine is a pure function** `evaluate(proposal, context, policy)`. ALL state (path-dependence, aggregate
  multi-agent exposure, agent budgets, response level, calendar) is a `RiskContext` INPUT, captured in the
  record, replayed exactly. (ADR-0006) A stateful engine object was rejected: it cannot replay deterministically.
- Money/quantity/ratio are **DecimalStr JSON strings**, normalised at validation (`"2.40"` → `"2.4"`), so equal
  money hashes equally. JSON numbers rejected. No `float` anywhere in the decision path.
- Time is an input (`context.evaluated_at`). Only the execution gate reads an injectable clock.
- `proposal.reasoning` is audit-only and excluded from `proposal_id`; only `mizan/advisory`, `mizan/audit`,
  `mizan/console` may read it. Injection text provably changes nothing.
- Advisory can only CONCUR / REDUCE / REJECT. The contract has no vocabulary for "more" (E1).
- Ledger: append-only **at the storage layer** (SQLite triggers now, Postgres triggers in `infra/`), one chain and
  one file per tenant, `ZERO_HASH` start, `audit_hash = sha256(canonical(record minus audit_hash))`.
  Verification works offline with no Mizan code — see `contracts/CANONICAL.md` §6.
- Paper only: every `environment` enum is `["paper"]`; `ALPACA_PAPER` != true raises `LiveTradingForbidden`
  at construction, before credentials are read or a socket opens.
- Execution gate order (E4 is last on purpose): enabled → auth valid → idempotency → TOCTOU re-evaluate
  (+ response-level escalation) → consume auth → auth valid again → **kill switch** → dry-run/submit.
  Never resize (E5): fresh risk supporting less is `REAUTHORIZATION_REQUIRED`.
- Policy enabling a check the engine does not implement → refused at load (`CHECK_NOT_IMPLEMENTED`).
  Missing state for an enabled check → blocking REJECT (E2 extended per R-RUIN-4).
- JSON Schemas are **generated** from the pydantic models by `scripts/generate_schemas.py`; CI diffs them so
  drift is impossible. One definition, not two that quietly diverge.
- Say **"decision replay"**, never bare "replay", in user-facing text (collides with retail chart-replay tools).
- No competitor claim in any doc or deck without a working link.

## Environment
Python 3.12.4 (miniconda), pytest 9, pydantic 2.13.4, jsonschema 4.26, hypothesis 6.157, ruff.
**Docker daemon NOT running** → compose and Postgres DDL are authored and statically tested only; CI covers them.
No `gh` CLI. Run tests: `python -m pytest -q -p no:cacheprovider <dir>`.

## Status after the Sprint 1 checkpoint (commit a5aab5b)
| Suite | Result |
|---|---|
| Invariants | PASS 2 (15 no-binary-float, 16 no-live-trading), PENDING-IMPL 16, **BLOCKING 0** |
| Contracts | 42 passed |
| Legacy `app/` | 354 passed, 1 skipped (unchanged) |
| Security | 63 passed, 41 xfail (documenting legacy redaction gaps the new `redact` must cover) |
| Secret scan | clean on working tree and on full git history |

`tests/invariants/conftest.py` prints an **INVARIANT STATUS** block classifying each of the 18 as PASS /
PENDING-IMPL (blocked only on a stub's `NotImplementedError`) / BLOCKING. Read it at every checkpoint.
**BLOCKING > 0 means no merges, in any lane.**

## Sprint log
- **S1** — L0 foundations. All four S1 agents died on one shared session rate limit; the Orchestrator finished
  the critical path directly (12 lane stubs, schema generator, 9 schemas, 2 real bug fixes). Contracts frozen.
- **S2 (in flight)** — L1 policy+risk · L2a governor+advisory+authorization · L2b audit+replay+verify-chain CLI ·
  L0-finish infra tests+ADRs+CONTRIBUTING.

## Open items
- L3 (execution gate, adapters, SDK, API) and L4 (console) not yet dispatched — next wave.
- L5 sweeps 3–7 not yet run (1–2 done against legacy `app/`). Sweeps 6 and 7 must re-run after every L2/L3 merge.
- `ledger/requests.md` REQ-1: ~124 ruff style findings under `mizan/`; each lane fixes its own paths.
- Killer demo (Master Plan §11) not yet wired end to end.
- Docker/Postgres path never executed locally.
