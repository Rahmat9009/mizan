"""Write a real, on-disk, hash-chained ledger of governed decisions. No credentials, no network.

The point of this file is what it does NOT need. It builds a ledger a third party can be handed - a
SQLite file per tenant, append-only at the storage layer - using a MockBroker and fixture market data,
so the reproducibility claim can be checked by anyone, on any machine, without an Alpaca key and
without asking us for access to anything.

    python examples/seed_ledger.py --out ./evidence/ledger
    python -m mizan.replay --ledger ./evidence/ledger

The decisions are deliberately a mix - an APPROVE, a REDUCE and a REJECT - because a replay proof over
four identical APPROVEs would demonstrate much less. A REJECT that reproduces bit-for-bit is the
interesting case: it means the REASON a trade was refused is reproducible, not merely the fact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mizan import authorization, governor, risk  # noqa: E402
from mizan.audit import SqliteLedger  # noqa: E402
from mizan.contracts import TradeProposal  # noqa: E402

TENANT = "tenant-a"


def _scenarios():
    """(label, proposal, context, policy) - each exercising a different verdict."""
    from tests.fixtures import make_option_proposal, make_proposal
    from tests.invariants._support import path_and_aggregate_policy, unstressed_context

    policy = path_and_aggregate_policy()
    context = unstressed_context(policy)

    def option(strategy: str, legs: list[dict]) -> TradeProposal:
        payload = make_option_proposal().model_dump(mode="json")
        payload.pop("proposal_id", None)
        payload.pop("total_quantity", None)
        base = payload["legs"][0]
        payload["strategy"] = strategy
        payload["legs"] = [{**base, "leg_index": i, **leg} for i, leg in enumerate(legs)]
        return TradeProposal.build(**payload)

    call = lambda side, strike: {"side": side, "contract_type": "call", "strike": strike}  # noqa: E731

    return [
        ("equity buy, expected APPROVE", make_proposal(), context, policy),
        (
            "defined-risk bull call spread, expected APPROVE",
            option("bull_call_spread", [call("buy", "230"), call("sell", "235")]),
            context,
            policy,
        ),
        (
            "UNDEFINED-RISK spread of two shorts, expected REJECT (F-31)",
            option("bull_call_spread", [call("sell", "230"), call("sell", "235")]),
            context,
            policy,
        ),
        (
            "naked short call as custom, expected REJECT (F-31)",
            option("custom", [call("sell", "230")]),
            context,
            policy,
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("evidence/ledger"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    from tests.fixtures import FIXED_NOW

    tenant = SqliteLedger(args.out).for_tenant(TENANT)
    print(f"seeding {args.out}/{TENANT}.sqlite  (no credentials, no network)")

    for label, proposal, context, policy in _scenarios():
        evaluation = risk.evaluate(proposal, context, policy)
        decision = governor.govern(proposal, evaluation, policy, None, context=context)
        auth = None
        if decision.verdict != "REJECT":
            auth = authorization.issue(decision, proposal, policy, now=FIXED_NOW, context=context)
        record = tenant.append(
            proposal=proposal,
            risk_context=context,
            risk_evaluation=evaluation,
            governor_decision=decision,
            policy_snapshot=policy,
            authorization=auth,
            recorded_at=FIXED_NOW,
        )
        codes = ", ".join(str(c) for c in decision.reason_codes) or "-"
        print(f"  seq {record.sequence}  {decision.verdict:<8} {label}")
        print(f"          codes: {codes}")

    chain = tenant.verify_chain()
    print()
    print(f"chain: ok={chain.ok} length={chain.length}")
    print(f"now run:  python -m mizan.replay --ledger {args.out}")
    return 0 if chain.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
