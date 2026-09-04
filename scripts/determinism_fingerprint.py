"""Emit a canonical fingerprint of Mizan's deterministic decision path (M2 / Hard Rule A1).

The claim this proves is the product: the same inputs produce a byte-identical verdict, everywhere,
always. That claim is broken by things no in-process test can see - hash randomisation changing map
iteration order, a locale whose decimal separator is a comma, a timezone shifting a timestamp, a
different interpreter or library build. Every one of those is a PROCESS or MACHINE property, so the
only way to test them is to run in another process, or on another machine, and compare.

This script is that comparison point. It is deliberately standalone - no pytest, no network, no
credentials, no clock, no randomness - so it can be copied to a second machine and run directly:

    python scripts/determinism_fingerprint.py --out fingerprint-a.json
    python scripts/determinism_fingerprint.py --check fingerprint-a.json

Identifiers derived from content (evaluation_id, verdict_hash) are IN the fingerprint. Identifiers
that are deliberately random per decision (decision_id, auth_id - uuid7) are OUT of it: they are
expected to differ and are excluded from verdict_hash by the contract for the same reason.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import platform
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mizan import governor, risk  # noqa: E402
from mizan.contracts.canonical import canonical_json  # noqa: E402

FINGERPRINT_SCHEMA = "mizan.determinism-fingerprint/1"


def _scenarios() -> list[tuple[str, Any, Any, Any]]:
    """Fixed scenarios spanning the paths where nondeterminism actually hides.

    Chosen for what each one stresses, not for coverage theatre: free-text handling, Decimal
    arithmetic and rounding, dict iteration over the exposure maps, the drawdown ladder, the
    multi-leg option path, and the SELL side.

    That last one was added after this fingerprint failed to notice a real behaviour change. Fixing
    F-30 altered what four capital checks do with every short, and the fingerprint still MATCHED,
    because not one of the five scenarios contained a sell leg. A behavioural fingerprint blind to an
    entire side of the market is not measuring behaviour, and worse, the engine-version control that
    depends on it would have let the change ship without a version bump.

    The lesson generalises past this one gap: a fingerprint is only evidence about the shapes it
    covers, so a new class of proposal belongs here at the same time as the code that handles it.
    """
    from tests.fixtures import (
        injection_reasoning,
        make_option_proposal,
        make_path_state,
        make_proposal,
    )
    from tests.invariants._support import full_book, path_and_aggregate_policy, unstressed_context

    policy = path_and_aggregate_policy()
    baseline = unstressed_context(policy)
    equity = baseline.portfolio_snapshot.equity
    limit = policy.aggregate.max_portfolio_exposure_pct

    return [
        ("pass_baseline", make_proposal(), baseline, policy),
        (
            "reduce_by_drawdown",
            make_proposal(),
            unstressed_context(policy, path_state=make_path_state(current_drawdown_pct="0.12")),
            policy,
        ),
        (
            "reject_by_aggregate",
            make_proposal(),
            unstressed_context(policy, aggregate_state=full_book(equity, limit)),
            policy,
        ),
        ("option_multi_leg", make_option_proposal(), baseline, policy),
        (
            "injected_free_text",
            make_proposal(reasoning=injection_reasoning()),
            baseline,
            policy,
        ),
        (
            # An OPENING short against a book that holds nothing to offset it - the shape whose
            # capital treatment F-30 was about, and the one no other scenario reaches.
            "opening_short",
            make_proposal(
                intent="open",
                strategy="long_equity",
                legs=[
                    {
                        "leg_index": 0,
                        "side": "sell",
                        "contract_type": None,
                        "strike": None,
                        "expiry": None,
                        "quantity": "10",
                        "limit_price": "228.50",
                        "order_type": "limit",
                    }
                ],
            ),
            baseline,
            policy,
        ),
    ]


def compute() -> dict[str, Any]:
    """The fingerprint. Contains only values the product promises are reproducible."""
    from mizan.contracts.canonical import ENGINE_VERSION, library_versions

    results = []
    for name, proposal, context, policy in _scenarios():
        evaluation = risk.evaluate(proposal, context, policy)
        decision = governor.govern(proposal, evaluation, policy, None, context=context)
        results.append(
            {
                "scenario": name,
                "evaluation_id": evaluation.evaluation_id,
                "evaluation_verdict": evaluation.verdict,
                "evaluation_reason_codes": [str(c) for c in evaluation.reason_codes],
                "evaluation_recommended_quantity": evaluation.recommended_quantity,
                "check_ids_in_order": [c.check_id for c in evaluation.checks],
                "check_passed_in_order": [c.passed for c in evaluation.checks],
                "decision_verdict": decision.verdict,
                "decision_reason_codes": [str(c) for c in decision.reason_codes],
                "authorized_total_quantity": decision.authorized.total_quantity,
                "verdict_hash": decision.verdict_hash,
                "policy_hash": policy.policy_hash,
            }
        )

    # HASHED: only what the product promises is reproducible everywhere. engine_version is in, because a
    # version bump is *allowed* to change verdicts and must therefore change the fingerprint.
    body = {
        "schema": FINGERPRINT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "scenarios": results,
    }
    fingerprint = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    # NOT HASHED: library_versions legitimately differs between machines (a pydantic patch release is not
    # a determinism failure). It is recorded for diagnosis so a genuine mismatch can be attributed, but
    # including it would make every cross-machine comparison fail for the wrong reason.
    return {**body, "fingerprint": fingerprint, "diagnostics": {"library_versions": library_versions()}}


def environment() -> dict[str, Any]:
    """Diagnostics only - deliberately NOT part of the fingerprint. These are expected to differ."""
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "<unset>"),
        "LC_ALL": os.environ.get("LC_ALL", "<unset>"),
        "LANG": os.environ.get("LANG", "<unset>"),
        "TZ": os.environ.get("TZ", "<unset>"),
        "preferred_encoding": locale.getpreferredencoding(False),
        "cwd": os.getcwd(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the fingerprint to this file")
    parser.add_argument("--check", type=Path, help="compare against a previously written fingerprint")
    parser.add_argument("--env", action="store_true", help="also print environment diagnostics")
    args = parser.parse_args()

    body = compute()
    if args.env:
        print(json.dumps({"environment": environment()}, indent=2, sort_keys=True), file=sys.stderr)

    if args.out:
        args.out.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        print(body["fingerprint"])
        return 0

    if args.check:
        expected = json.loads(args.check.read_text(encoding="utf-8"))
        if expected.get("fingerprint") == body["fingerprint"]:
            print(f"MATCH {body['fingerprint']}")
            return 0
        print(f"MISMATCH\n  expected {expected.get('fingerprint')}\n  actual   {body['fingerprint']}")
        for old, new in zip(expected.get("scenarios", []), body["scenarios"], strict=False):
            for key in sorted(set(old) | set(new)):
                if old.get(key) != new.get(key):
                    print(f"  {old.get('scenario')}.{key}: {old.get(key)!r} != {new.get(key)!r}")
        return 1

    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
