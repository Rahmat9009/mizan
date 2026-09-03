"""``python -m mizan.replay`` - the decision-replay command line (Hard Rule A1).

Hermetic by construction: no credentials, no network, no broker, no clock. It builds its own chain
from the fixture scenarios, replays every record, and compares the deterministic fingerprint against
the reference committed at the repository root. That makes it safe to run on any CI runner, which is
the point - the cross-machine determinism matrix is exactly this command on several machines at once.

    python -m mizan.replay --all --assert-identical

Exit code 0 means every decision replayed to an identical verdict AND the fingerprint matched.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from mizan.audit import InMemoryLedger
from mizan.replay import replay

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "determinism-reference.json"


def _build_chain(count: int):
    """A chain of records produced by the REAL engine. Imported lazily; the package never needs tests.

    ``append_engine_record`` runs risk.evaluate and governor.govern for real. The fixture builders
    (``append_record``) fabricate a canned GovernorDecision instead, which replay then correctly
    recomputes into a different verdict_hash - a property of the fixture, not a determinism failure.
    Replaying fabricated records here would report a false positive on every run.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tests.fixtures import FIXED_NOW, make_option_proposal, make_proposal
    from tests.invariants._support import append_engine_record

    tenant = InMemoryLedger().for_tenant("tenant-a")
    for index in range(count):
        proposal = make_option_proposal() if index % 4 == 3 else make_proposal()
        append_engine_record(tenant, recorded_at=FIXED_NOW, proposal=proposal)
    return tenant


def _replay_all(tenant, verbose: bool) -> tuple[int, int]:
    records = tenant.list(limit=1000)[::-1]
    identical = 0
    for record in records:
        result = replay(record)
        if result.identical:
            identical += 1
            if verbose:
                print(
                    f"  OK       {record.sequence} {result.original_verdict} "
                    f"{result.original_verdict_hash[:16]}"
                )
        else:
            print(
                f"  DIFFERS  seq={record.sequence} decision={result.decision_id}\n"
                f"      verdict {result.original_verdict!r} -> {result.replayed_verdict!r}\n"
                f"      hash    {result.original_verdict_hash} -> {result.replayed_verdict_hash}\n"
                f"      detail  {result.detail}"
            )
    return identical, len(records)


def _fingerprint_matches(verbose: bool) -> bool:
    """Compare against the committed reference by running the standalone script, so CI exercises the
    same entry point an engineer would run by hand on a second machine."""
    if not REFERENCE.exists():
        print(f"  MISSING  no reference at {REFERENCE}; generate it with scripts/determinism_fingerprint.py")
        return False
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "determinism_fingerprint.py"),
            "--check",
            str(REFERENCE),
        ],
        capture_output=True,
        text=True,
    )
    if verbose or proc.returncode != 0:
        print("  " + (proc.stdout or proc.stderr).strip().replace("\n", "\n  "))
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mizan.replay", description=__doc__)
    parser.add_argument("--all", action="store_true", help="replay every record in the built chain")
    parser.add_argument(
        "--assert-identical",
        action="store_true",
        help="exit non-zero unless every replay is identical and the fingerprint matches",
    )
    parser.add_argument("--count", type=int, default=8, help="records to build and replay (default 8)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.all:
        parser.error("nothing to do: pass --all")

    print(f"decision replay: building {args.count} records")
    tenant = _build_chain(args.count)

    chain = tenant.verify_chain()
    print(f"chain: ok={chain.ok} length={chain.length}")

    identical, total = _replay_all(tenant, args.verbose)
    print(f"replayed: {identical}/{total} identical")

    print("fingerprint:")
    fingerprint_ok = _fingerprint_matches(args.verbose)

    ok = chain.ok and identical == total and total > 0 and fingerprint_ok
    print("RESULT: " + ("IDENTICAL" if ok else "NOT IDENTICAL"))
    if args.assert_identical and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
