# CURRENT AIM

Build the Mizan core: a deterministic, replayable, hash-chained pre-trade governance layer that sits between AI trading agents and a broker.

**Definition of victory for this run:**

```
A developer runs TradingAgents, adds ~10 lines, and every order
their agents produce becomes:
   - checked against a versioned policy
   - governed (APPROVE / REDUCE / REJECT) with reason codes
   - written to an append-only hash-chained ledger
   - deterministically replayable to an identical verdict
   - executable only on Alpaca PAPER, behind a kill switch
```

Written once at boot. Not rewritten mid-run. Disagreements with the aim go to `ledger/escalations.md`.
