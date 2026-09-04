# MIZAN — UI/UX SPECIFICATION

### Consolidating three proposals, with corrections

**Version:** 1.0
**Date:** 2 September 2026
**Inputs:** Doc A ("Provenance Card"), Doc B ("Decision Card / Mizan Lens"), Doc C ("Auto-Redline Diff Card")
**Companion to:** `MIZAN-MASTER-PLAN-v2.md`, `MIZAN-FEATURE-VERDICT.md`, `MIZAN-RISK-CANON.md`

---

## 0. THE CONVERGENCE

All three documents, written independently, arrived at the same hero element:

| Doc | Their name for it |
|---|---|
| A | "Proposed → Approved" |
| B | "AI Wanted / Mizan Allowed / Executed" |
| C | "The Diff Card" |

**Three independent analyses landing on one element is the strongest design signal available.** Build it. It is the signature interaction and everything else is navigation around it.

But **do not build it as any of the three described it.** Two of them contain errors that contradict the architecture.

---

## 1. THREE CORRECTIONS — read before designing anything

### ✗ CORRECTION 1: It is not a "negotiation"

Doc C frames the card as *"the negotiation between the AI's greed and Mizan's risk management"* and calls it "Visual Trade Negotiation."

**Kill that framing.** It contradicts the entire product thesis and it will lose you enterprise deals.

A negotiation implies two parties with standing. Mizan's architecture — and its regulatory argument, and Hard Rule E1 — is that the LLM has **no authority whatsoever**. It proposes. It cannot bargain. The deterministic engine decides.

Say "negotiation" to a compliance officer and they hear *the AI has a seat at the table*. That is precisely the fear you are selling against.

```
✗ NEGOTIATION    "AI wanted 50, Mizan countered with 10"
✓ AUTHORIZATION  "Agent requested 50. Policy authorized 10."
```

**Correct mental model: a permit application, not a haggle.** The GitHub-PR analogy Doc C reaches for is right in *form* — a visual diff — and wrong in *meaning*. A PR implies a proposal that a reviewer may accept. Better: **a customs declaration.** You declare what you intend; the authority states what is permitted.

**Approved vocabulary:** Requested · Authorized · Executed. Never: wanted, asked for, negotiated, agreed, countered, allowed *(too permissive)*, granted *(implies discretion)*.

### ✗ CORRECTION 2: Mizan does not adjust prices

Doc C's mockup shows:
```
~~$2.50~~ → $2.10    "Limit price adjusted to current market mid-price"
```

**This must never ship.** Repricing is a *trading* decision, not a *risk* decision, and it breaks the product in three ways:

1. **Liability.** If Mizan repriced and the fill was poor, the loss is attributable to Mizan. You have moved from governance to execution and acquired responsibility for outcomes.
2. **It violates the authority model.** Reducing quantity enforces a limit. Changing a price expresses a *view on value*. Mizan has no views.
3. **It breaks Hard Rule E5.** Silent resizing is prohibited; silent repricing is worse.

**Mizan's verdict vocabulary is exactly three values: APPROVE · REDUCE · REJECT.** Reduce means quantity. Nothing else changes. If a price is unacceptable, the verdict is REJECT with a reason code, and the agent submits a new proposal.

### ✗ CORRECTION 3: Do not display raw agent confidence as a headline

Doc B's card shows `Confidence 87%` prominently.

An agent's self-reported confidence is an **estimate with unknown error**. The Risk Canon (R-KELLY-2/3) requires it to be haircut before use precisely because treating it as a true probability is the classic over-betting failure. Displaying it as a large clean number teaches users to trust a number your own engine distrusts.

```
✗  Confidence  87%

✓  Agent confidence   87%  →  calibrated  61%
   This agent has historically been overconfident
   by 26 points across 340 decisions.
```

**Same objection applies to Doc B's `Risk: LOW` badge.** A single-word risk summary is the thing Marks and Taleb warn about, and it edges toward advice. Show the *drivers*, not a verdict on safety.

### Minor fixes

- Doc A's mockup hardcodes a model name in the card. Model binding belongs to agent identity, not policy (Master Plan C9). Display it as data, never as a configured field.
- Doc C's JSON uses `"governor_reason"` as free prose. Hard Rule A4 requires machine-readable reason codes. Prose is a *rendering* of a code, never the source of truth.

---

## 2. THE HERO ELEMENT

```
┌──────────────────────────────────────────────────────────┐
│  NVDA · PUT SPREAD                   10:42:18   #4f2a9c  │
│  ● REDUCED                              ✓ chain verified │
├──────────────────────────────────────────────────────────┤
│                                                          │
│   REQUESTED          AUTHORIZED          EXECUTED        │
│   ──────────         ──────────          ────────        │
│   10 contracts   →   6 contracts     →   6 filled        │
│   $4,200             $2,520              $2,518          │
│                                                          │
│   ⚠ Reduced 10 → 6                                       │
│     ▸ CONCENTRATION_LIMIT                                │
│                                                          │
│   Agent trader-07 · Policy v18 · Model <provider/ver>    │
│   Authorization expired 14:32:05 · used at 14:31:58      │
│                                                          │
│   [ Why? ]    [ Replay ]    [ Evidence ]                 │
└──────────────────────────────────────────────────────────┘
```

**Three columns, always.** Requested → Authorized → Executed. The gap between column one and column two *is the product*. If a user learns one thing about Mizan, it is that this gap exists and is enforced.

**The authorization line is new** and none of the three documents included it. It is the most differentiated thing on the card — see §5.2.

---

## 3. THE "WHY?" PANEL — the best idea in any of the three documents

Doc B's visual explanation is the single strongest element proposed. Build it exactly:

```
Mizan reduced this order from 10 to 6 contracts.

PORTFOLIO CONCENTRATION — limit 20%

  Current          ██████████████████░░   18.7%   ✓
  With 10          █████████████████████  22.4%   ✕
  With 6           ███████████████████░   19.8%   ✓

  Authorized: 6 contracts
  Reason code: CONCENTRATION_LIMIT
  No human intervention required.
```

**Why this works:** it replaces risk vocabulary with a picture. A user who has never heard the word "concentration" understands the bar that goes past the line. It is also *honest* — it shows the actual arithmetic rather than an LLM's paraphrase of it.

**Rule: the Why panel is rendered from the deterministic check output only.** Never from an LLM explanation. The check already produced `threshold`, `actual`, and `reason_code` — the panel is a rendering of those three fields. This keeps the explanation provably identical to the enforcement, which is the whole point.

---

## 4. PROGRESSIVE DISCLOSURE — the governing principle

Doc B's five-level model is correct and should govern the entire product:

```
L1  VERDICT      "REDUCED"                        — 1 second
L2  WHY          the bar chart above              — 10 seconds
L3  DETAIL       all checks, thresholds, actuals  — 1 minute
L4  EVIDENCE     full decision record, provenance — 5 minutes
L5  RAW          immutable record + hash chain    — export
```

**Every screen obeys this.** No screen shows L3 before L1. The failure mode of every governance product is opening at L4 and calling it transparency.

---

## 5. WHAT ALL THREE DOCUMENTS MISSED

Five gaps. Two are the features that most differentiate you.

### 5.1 ✱ Aggregate / multi-agent exposure — no screen exists

The Bank of England named herding among trading agents as a systemic concern. The Risk Canon calls aggregate exposure your highest-differentiation feature. **Not one of the three proposals has a screen for it.**

Doc B's "portfolio by agent" comes closest but only shows attribution, not correlation.

```
┌──────────────────────────────────────────────────────────┐
│  AGENT CROWDING                            ⚠ ELEVATED    │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  4 of 7 agents hold correlated same-direction exposure   │
│                                                          │
│    trader-02  ████████  NVDA                             │
│    trader-05  ██████    AMD      ┐                       │
│    trader-07  ████████  NVDA     ├ semiconductors  71%   │
│    trader-11  █████     AVGO     ┘                       │
│                                                          │
│  Each agent is within its individual limits.             │
│  Aggregate sector exposure exceeds 60% guidance.         │
│                                                          │
│  Model concentration:  82% of exposure from one provider │
│  Signal concentration: 64% from one data source          │
│                                                          │
│  Simultaneous exit would take 4.2 days at normal volume. │
│                                                          │
│  [ Tighten aggregate limits ]   [ View detail ]          │
└──────────────────────────────────────────────────────────┘
```

**The sentence that sells the product:** *"Each agent is within its individual limits."* That is the failure no other system can see.

### 5.2 ✱ Authorization expiry — the most arresting demo moment, unrendered

From the Feature Verdict: an agent confidently holding a permission that expired. No proposal renders it.

```
┌──────────────────────────────────────────────────────────┐
│  AUTHORIZATION #a7f3                                     │
│                                                          │
│  6 NVDA 580P · max $2.10 · paper-01 · policy v18         │
│                                                          │
│  ████████████████████░░░░░░  expires in 4s               │
│                                                          │
│  Bound to portfolio state  9c73f2…                       │
│  Bound to market state     4a1e8b…                       │
│                                                          │
│  ⓘ If either state changes, this authorization becomes   │
│    invalid — even before it expires.                     │
└──────────────────────────────────────────────────────────┘
```

Then the moment that lands:

```
  ✕ AUTHORIZATION INVALID

  Portfolio state changed at 14:32:06.
  Agent trader-07 attempted execution at 14:32:07.

  The agent believed it was approved.
  It was approved for a state of the world that no longer exists.

  Result: REAUTHORIZATION_REQUIRED
```

**A live countdown bar is worth more in a demo than any static card.** It makes an abstract security property physical.

### 5.3 Graduated response level — no indicator

The Risk Canon replaced the binary kill switch with a six-level ladder. The UI has no persistent indicator of which level the system is at. Add it to the global header:

```
MIZAN     ● LEVEL 0 · NORMAL          [ ESCALATE ]  [ ⏻ FULL STOP ]
```

### 5.4 Kill switch — mentioned nowhere in three UX documents

It must be reachable from every screen, guarded by a confirm step, and visually distinct from everything else. Also: it must render *state*, not just an action — "FULL STOP ACTIVE, engaged 14:32:09 by rahmat@…, 3 agents halted."

### 5.5 The quiet state — the hardest design problem here

**Ninety-nine percent of the time, nothing is wrong.** Every mockup in all three documents shows a system with problems. Design the boring day, or the product feels dead in normal operation and users stop opening it.

The answer: make the quiet state *itself* the proof of value.

```
        TODAY

        1,842 decisions governed
        1,737 approved · 74 reduced · 31 blocked

        $4.82M requested · $3.17M authorized
        $1.65M of exposure prevented

        Nothing needs your attention.

        Chain verified · 1,842 / 1,842
```

`$1.65M of exposure prevented` is the number that renews the contract. It converts an uneventful day from *"nothing happened"* into *"here is what we stopped."* Put it on the home screen permanently.

---

## 6. FEATURE RANKING

| Feature | Source | User value | Differentiation | Build | Priority |
|---|---|---|---|---|---|
| **Requested → Authorized → Executed** | all three | ★★★★★ | ★★★★★ | Low | **P0** |
| **Visual "Why?" panel** | B | ★★★★★ | ★★★★ | Low | **P0** |
| **Replay proof badge** | A | ★★★★★ | ★★★★★ | Med | **P0** |
| **Aggregate crowding screen** | *missing* | ★★★★★ | ★★★★★ | High | **P0** |
| **Authorization countdown** | *missing* | ★★★★ | ★★★★★ | Low | **P0** |
| **Quiet-state home** | *missing* | ★★★★★ | ★★★★ | Low | **P0** |
| **Attention Inbox** | B | ★★★★★ | ★★★★ | Med | **P1** |
| **Response-level indicator** | *missing* | ★★★★ | ★★★ | Low | **P1** |
| **Kill switch UI** | *missing* | ★★★★ | ★★ | Low | **P1** |
| **Policy "What changed?" simulator** | B | ★★★★ | ★★★★★ | High | **P1** |
| **Replay as step-through timeline** | B | ★★★★ | ★★★★ | Med | **P1** |
| **Agent control-health panel** | B | ★★★★ | ★★★ | Med | **P1** |
| **Portfolio-by-agent map** | B | ★★★★ | ★★★★ | Med | **P2** |
| **Persona switcher** | B | ★★★ | ★★★ | Low | **P2** |
| **Mizan Lens (4-layer view)** | B | ★★★★ | ★★★★ | Med | **P2** — see note |
| Risk thermometer | B | ★★★ | ★ | Low | **P3** — see §1.3 |
| Raw audit log view | A | ★★ | ★ | Low | **P3** |

**Note on Mizan Lens:** Intent · Risk · Authority · Outcome is an elegant framing, but it duplicates the hero card at a different zoom level. Ship the hero card first and see whether users actually want the Lens, or whether the card already answered the question. Do not build both in the same sprint.

---

## 7. DESIGN SYSTEM CONSTRAINTS

**Accessibility — non-negotiable.**
All three documents encode meaning in colour alone (red strikethrough, green approved, colour-coded reason badges). Roughly 1 in 12 men has a colour-vision deficiency, and this is a product sold to trading desks.

```
✗  red text = reduced,  green text = approved
✓  ▼ REDUCED (colour + icon + word)
   ● APPROVED
   ✕ REJECTED
```

Every status carries **icon + word + colour**. Never colour alone. Never strikethrough alone.

**Number formatting.** Money and quantity are decimal strings end-to-end (Hard Rule A6). Never let the frontend parse them into JavaScript floats — `0.1 + 0.2` will eventually appear in a compliance screenshot.

**Timestamps.** UTC with explicit label, plus local on hover. Never bare local time — an audit record read in three timezones must be unambiguous.

**Density.** Two modes, as Doc B suggested: dark for traders and ops, light for risk and compliance. But do not treat this as theming only — compliance users need higher information density and print-friendly layouts; traders need glanceability.

**Never render an LLM explanation as the primary "why."** The deterministic check output is the explanation. An LLM paraphrase may appear at L4, clearly labelled advisory, never at L1 or L2.

---

## 8. BUILD ORDER

**Sprint 1 — Streamlit, disposable.** Doc C is right that the hero card is a 30-minute build. Do it in Streamlit to prove the backend emits the right shape. Label it internal-only. Never show it to a customer or investor.

**Sprint 2 — the P0 five.** Hero card · Why panel · replay proof badge · authorization countdown · quiet-state home.

**Sprint 3 — aggregate crowding screen.** Depends on L1's aggregate check layer landing first.

**Sprint 4 — Attention Inbox + response level + kill switch.**

**Sprint 5+ — P1 remainder.**

**Backend contract the frontend needs from day one:**
```json
{
  "verdict": "REDUCE",
  "requested":  {"quantity": 10, "notional": "4200.00"},
  "authorized": {"quantity": 6,  "notional": "2520.00"},
  "executed":   {"quantity": 6,  "notional": "2518.40"},
  "reason_codes": ["CONCENTRATION_LIMIT"],
  "checks": [{
    "check_id": "concentration",
    "passed": false,
    "threshold": "0.20",
    "actual_current": "0.187",
    "actual_if_requested": "0.224",
    "actual_if_authorized": "0.198"
  }],
  "authorization": {"expires_at": "...", "bound_state_hash": "..."},
  "chain": {"verified": true, "position": 48213}
}
```

`actual_if_requested` and `actual_if_authorized` are what make the Why panel possible. **Tell L1 and L2 now** — those fields do not exist in the current contract, and retrofitting them means recomputing historical decisions.

---

## 9. THE 60-SECOND DEMO

```
1.  Home screen, quiet state.
    "1,842 decisions governed today. $1.65M of exposure prevented."

2.  Click a REDUCED decision.
    Requested 10 → Authorized 6 → Executed 6.

3.  Click "Why?"
    The bar crosses the line at 10. It doesn't at 6.
    Nobody needs risk vocabulary explained.

4.  Click "Replay."
    ✓ Verdict identical · ✓ Chain verified from genesis · 12ms

5.  Open the authorization panel. Watch the countdown hit zero.
    "The agent still believes it's approved. It isn't."

6.  Open Agent Crowding.
    "Each agent is within its individual limits."

7.  Hit FULL STOP. Everything halts.
```

Steps 3, 5 and 6 are the ones people remember. Step 4 is the one that closes a compliance buyer.

---

## 10. THE SENTENCE

Doc A proposed: *"I clicked a button and it proved the decision was legit."*

Closer, and truer to the architecture:

> **"I could see exactly what my AI tried to do, and exactly what it was allowed to do — and I could prove it."**

Requested. Authorized. Proven. Everything else is navigation.
