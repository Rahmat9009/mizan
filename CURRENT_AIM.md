# CURRENT AIM — Alpaca hackathon submission

Maximize the quality of the submission due today. Judged on P&L, Technology
Implementation, Creativity, Presentation, Social.

## The settled conclusion — do not re-derive it

**P&L is not winnable, so do not chase it.** The most mature comparable system
reports a profit factor of 0.22 over 69 closed trades. Our own open spread is
EV about zero: 11.4% credit-to-width, roughly 0.8 sigma, in a LOW realized-vol
regime that our own shadow signal scores at the 6th percentile.

Taking more risk to move a one-day paper number is therefore negative expected
value on the scoreboard AND on the story. **Optimize Technology, Creativity and
Presentation, and make the P&L story honest.**

## What we actually have that is rare

- A gate that refuses on real incomplete data instead of guessing.
- Alpaca's official MCP server exposes 72 tools, **seven of which liquidate an
  account with no decision recorded**. Mizan makes 42 of 53 unreachable.
- 12/12 decisions reproduce bit-for-bit from the ledger via one credential-free
  command — a judge can verify the central claim with no key and no network.
- 26 invariants, including two (INV-25/26) asserting a control must be able to
  FAIL and must carry evidence when it passes.

## Standing rules for this run

- Under-claim by default. Never claim a capability the ledgers do not support.
- No policy-shopping, no threshold tuning, no weakening a control to make
  progress. A recorded refusal is a deliverable, not a failure.
- Escalate to the human: any new order, any position change, any policy or
  invariant change, anything that makes a control easier to pass, and any
  public publication.

## Freeze

19:30 IST / 14:00 UTC. Stop building, commit, push both remotes, verify clean,
write FINAL_STATUS.md. Do not start what cannot be pushed before freeze.
