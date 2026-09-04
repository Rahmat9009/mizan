# MIZAN integration — Step 3 context + clone reconciliation

`web/docs/MIZAN-UX-SPEC.md` is product authority. The reference clones
(Galileo, Fiddler, Alpaca, Guardrails) define how sophisticated the
presentation should feel. Where they disagree, product semantics win.

This is the ledger of what the spec changed, what it contradicted, and what is
still outstanding.

---

## 1. Where the spec contradicted the existing implementation

### 1.1 The AI review was binding the size — corrected

The flagship NVDA case ran: deterministic engine caps at 24 shares, AI
contextual review recommends 20, Governor "takes the smaller of the two". The
authorized quantity was therefore set by a language model.

That is exactly the thing Correction 1 forbids. If the model's number is the
one that binds, the model has authority over size, and no amount of vocabulary
fixes the fact.

**Applied:** a deterministic `correlated_exposure` rule now binds at 20 shares
(NVDA and the existing AMD position share the same end-market; combined weight
at 40 shares reaches 18.3% against a 12.5% ceiling). The allocation ceiling
still binds at 24. Policy authorizes the smaller. The contextual review
concurs and tightens nothing.

The 40 → 20 headline is unchanged. The authority model is now correct, and the
"Why?" panel has a deterministic constraint to draw.

### 1.2 Verdict vocabulary

`APPROVE / REDUCE / REJECT` is the enum the engine emits. What a reader sees is
`APPROVED / REDUCED / REJECTED`, each with its glyph — ● ▼ ✕.

Renamed throughout: *Proposed* → **Requested**, *Approved* → **Authorized**.
Order table columns, the landing page worked example, the autonomy blurbs and
every aria-label. `authoris*` normalized to `authoriz*` so the product's fixed
vocabulary is spelled one way.

Never used anywhere: wanted, asked for, negotiated, agreed, countered, allowed,
granted.

### 1.3 Confidence as a headline

`strategyConfidence` was rendered bare (`confidence 0.78`) in list rows and the
case file. It now renders through `ConfidenceReadout`, which labels it as the
agent's own claim and shows the calibrated figure beside it where a calibration
record exists. Where none exists — the MSFT option case — the panel says so
rather than filling the space.

List rows no longer carry a confidence number at all. They carry the binding
reason code, which is what a triage row is for.

### 1.4 Progressive disclosure

The case file opened with the full check surface, the AI review, the gate list
and the audit button all visible — L3 and L4 material with the verdict as a
badge in a corner.

It now opens on `DecisionCard` (L1) and the `WhyPanel` (L2). Checks, evidence
and the raw record are L3–L5 behind a tablist. The case-file split with its
brass boundary survives inside L3.

### 1.5 Repricing

No UI anywhere offers or implies a price adjustment. `REDUCE` changes quantity
only; the `Decision` type carries a comment saying so, and the MSFT case states
explicitly that only the contract count changed.

---

## 2. What the spec added that did not exist

| Spec | Built |
|---|---|
| §2 hero: Requested → Authorized → Executed | `DecisionCard`, `AuthorizationLedger` |
| §3 visual "Why?" | `WhyPanel`, rendered only from check output |
| §4 L1–L5 disclosure | case-file restructure |
| §5.1 aggregate crowding | `/app/crowding` |
| §5.2 authorization expiry | `AuthorizationPanel`, and the JPM record |
| §5.3 response ladder | `ResponseLadder`, `ResponseLevelChip`, `ResponseBanner` |
| §5.4 kill switch as state | full-stop state block with who and when |
| §5.5 quiet-state home | `QuietState` at the top of `/app` |
| §7 decimal money | `lib/decimal.ts` |
| §8 backend contract | `DecisionOutcome`, `CheckMeasurement`, `Authorization`, `ChainStamp` |

---

## 3. Decisions taken where the spec left room

**Crowding is measured on strategies, not on a synthetic multi-agent desk.**
The spec's mock shows `trader-02 … trader-11`. This system has one agent
pipeline and several strategies, and `Position.sourceStrategy` already records
which produced each holding. So the clusters are built from real position
weights and reconcile exactly with `/app/portfolio`. Inventing a seven-trader
desk would have produced a screen that contradicts the portfolio page.

**Clusters are measured against *guidance*, not a hard limit.** That is what
makes the sentence true: every deterministic check passes, every agent is
inside its own limits, and the aggregate is still past the line. A hard limit
would have been caught per-trade and there would be nothing to show.

**Time-to-unwind reads Unavailable.** The spec's mock has "4.2 days at normal
volume". Nothing in this system supplies volume, so the figure is not
estimated. The empty state explains why.

**The countdown does not fake liveness.** `AuthorizationPanel` ticks once a
second for a permission whose status is `ACTIVE`. Every record in the demo
dataset is historical, so they render their consumed lifetime instead. The live
path exists and is exercised the moment a live record arrives.

**Response level names are provisional.** `MIZAN-RISK-CANON.md` is not in this
worktree. Levels 0–5 are NORMAL / ELEVATED / RESTRICTED / REDUCE ONLY /
HALT NEW / FULL STOP, to be reconciled with the canon when it lands.

**Decimal strings cover the new contract fields only.** `DecisionOutcome`
notionals, check thresholds and actuals, crowding weights and the quiet-day
figures are decimal strings end to end. The pre-existing numeric fields
(`marketValue`, `estimatedPrice`, …) are unchanged; converting them is a
separate pass and belongs with the backend wiring, not with a visual step.

---

## 4. What the backend must supply

None of these fields exist in the current contract. `actual_if_requested` and
`actual_if_authorized` cannot be backfilled without recomputing historical
decisions, so they need to land before history accumulates.

```json
{
  "verdict": "REDUCE",
  "requested":  {"quantity": 40, "notional": "7296.00"},
  "authorized": {"quantity": 20, "notional": "3648.00"},
  "executed":   {"quantity": 20, "notional": "3646.20"},
  "prevented_notional": "3648.00",
  "reason_codes": ["CORRELATED_EXPOSURE", "TRADE_ALLOCATION_LIMIT"],
  "checks": [{
    "check_id": "correlated_exposure",
    "passed": false,
    "reason_code": "CORRELATED_EXPOSURE",
    "unit": "ratio",
    "bound": "ceiling",
    "size_invariant": false,
    "threshold": "0.125",
    "actual_current": "0.061",
    "actual_if_requested": "0.183",
    "actual_if_authorized": "0.122"
  }],
  "authorization": {
    "id": "a7f3",
    "issued_at": "…", "expires_at": "…", "ttl_seconds": 120,
    "bound_portfolio_state": "9c73f2e1b4a80d5f",
    "bound_market_state": "4a1e8bd60c37f912",
    "used_at": "…", "status": "USED",
    "invalidated_at": null, "invalidation_code": null, "invalidation_detail": null
  },
  "chain": {
    "verified": true, "position": 48213,
    "record_hash": "…", "previous_hash": "…",
    "verified_at": "…", "verify_ms": 12
  }
}
```

New endpoints the adapter expects (`httpClient.ts` names them):

- `GET /governance/day` → `GovernanceDay`
- `GET /governance/response-level` → `ResponseState`
- `GET /governance/crowding` → `CrowdingReport`

`size_invariant` is worth stating explicitly: a volatility ceiling does not move
with order size, which is why it produces `REJECT` rather than `REDUCE`. The UI
says so, and it can only say so if the engine tells it.

---

## 5. Not yet built

- **Decision Replay as the proof badge.** `DecisionReplay` exists and steps
  through a proposal. The spec's §9 step 4 — "✓ Verdict identical · ✓ Chain
  verified from genesis · 12ms" — is a re-execution result, not a playback.
  `ChainStamp.verifyMs` is typed for it; nothing verifies yet.
- **Attention Inbox** (P1).
- **Policy "what changed?" simulator** (P1).
- **Agent control-health panel** (P1).
- **Landing page** has not been re-cut against the spec. It still uses the old
  worked example wording in places beyond the two labels changed here.
- **Persistent response-level state** is per-session UI state. It belongs to
  the backend's execution configuration.

---

## 6. Demo data honesty

Every figure in `src/data` carries a provenance tag and the tags are surfaced,
not hidden. Specifically:

- `GOVERNANCE_DAY` is tagged `DEMO` on the home screen. It aggregates a full
  session (1,842 decisions); the proposal ledger shows the most recent six,
  which is why the counts differ.
- `CROWDING` weights are the real `POSITIONS` weights. The two screens agree.
- Calibration records are tagged `DEMO`. The MSFT case has none and says so.
- `unwindDays` is `null` and renders `Unavailable`.
