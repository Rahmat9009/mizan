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

---

# SUBMISSION AIM — Alpaca hackathon (appended, does not replace the above)

The build aim above is unchanged and is still what the system is judged against
internally; the six integration tests in `tests/integration/test_the_demo_proves_the_aim.py`
read its bullets directly, which is why it is appended to rather than rewritten.

## The settled conclusion — do not re-derive it

**P&L is not winnable, so do not chase it.** The most mature comparable system
reports a profit factor of 0.22 over 69 closed trades. Our own open spread is
EV-negative on its own arithmetic: ~11.4% credit-to-width, roughly 0.8 sigma, in
a LOW realized-vol regime our shadow signal scores at the 6th percentile.

Taking more risk to move a one-day paper number is negative expected value on
the scoreboard AND on the story. **Optimize Technology, Creativity and
Presentation, and make the P&L story honest.**

## What we have that is rare

- A gate that refuses on real incomplete data instead of guessing.
- Alpaca's official MCP server exposes 72 tools, **seven of which liquidate an
  account with no decision recorded**. Mizan makes 42 of 53 unreachable.
- 12/12 decisions reproduce bit-for-bit via one credential-free command.
- 26 invariants, two of which (INV-25/26) assert a control must be able to FAIL
  and must carry evidence when it passes.

## Standing rules

- Under-claim by default. Never claim what the ledgers do not support.
- No policy-shopping, no threshold tuning, no weakening a control to progress.
  A recorded refusal is a deliverable.
- Escalate: any new order, any position change, any policy or invariant change,
  anything making a control easier to pass, any public publication.

## Freeze

19:30 IST / 14:00 UTC. Stop building, commit, push both remotes, verify clean,
write FINAL_STATUS.md.
